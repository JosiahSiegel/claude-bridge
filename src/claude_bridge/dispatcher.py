"""Core dispatcher: spawn `claude -p`, manage per-channel sessions.

Design points:

* **Per-channel session pinning.** Each "channel" maps to one Claude Code
  ``session_id``. The first dispatch on a channel starts a fresh session
  (with ``--session-id <uuid>`` if Claude Code supports it for ``-p``, else
  with no resume flag and we capture the id from the JSON output). Every
  subsequent dispatch passes ``--resume <session_id>``. We never use
  ``--continue``, which would race with concurrent dispatches because it
  always means "the most recent session in cwd".

* **Concurrency model.** Different channels run in parallel. Within a
  channel we hold an asyncio lock so messages stay ordered relative to the
  shared session.

* **Sync vs async dispatch.** ``dispatch`` runs synchronously (caller
  awaits the full ``claude -p`` round trip). ``dispatch_async`` kicks off
  in the background, returns a ``job_id`` immediately, and the caller
  polls via ``get_dispatch`` / ``wait_dispatch``. The async surface exists
  because MCP transports often impose a per-call ceiling (~60s) that's
  shorter than a real ``claude -p`` invocation, especially against a
  project with heavy ``.claude/`` config.

* **Atomic state writes.** The channel→session map is persisted to a JSON
  file written via a tempfile + ``replace`` so a crash during write never
  leaves a half-baked file.

* **Fail-soft.** Subprocess failures, timeouts, and JSON parse errors all
  return a structured error dict rather than raising. The MCP layer above
  surfaces them to Cowork as tool errors. Cancellation of an in-flight
  ``dispatch`` (via ``cancel_dispatch``) kills the subprocess before
  propagating ``CancelledError``, so we don't leave zombies.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class DispatchResult:
    ok: bool
    channel: str
    duration_ms: int
    result: str = ""
    session_id: str | None = None
    error: str | None = None
    exit_code: int | None = None
    raw: dict[str, Any] | None = None
    stderr: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ok": self.ok,
            "channel": self.channel,
            "duration_ms": self.duration_ms,
        }
        if self.ok:
            out["result"] = self.result
            out["session_id"] = self.session_id
            if self.raw is not None:
                out["raw"] = self.raw
        else:
            out["error"] = self.error or "unknown error"
            if self.exit_code is not None:
                out["exit_code"] = self.exit_code
        # stderr is included on both ok and !ok paths so callers can see
        # warnings (e.g. project MCP server disconnects) that didn't fail
        # the run but matter for diagnostics.
        if self.stderr:
            out["stderr"] = self.stderr
        return out


@dataclass
class Job:
    """Background dispatch job tracked by ``Dispatcher``.

    Each ``Job`` is persisted to disk so its state survives bridge process
    restarts. A live job has an ``asyncio.Task``; a job loaded from disk
    after a restart has ``task=None`` and a status reflecting what we
    knew last (``done``, ``cancelled``, ``error``, or ``orphaned`` if
    we lost track of it mid-run).
    """

    id: str
    channel: str
    started_at: float
    args: dict[str, Any] = field(default_factory=dict)
    status: str = "running"
    finished_at: float | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    task: "asyncio.Task[DispatchResult] | None" = field(default=None, repr=False)

    def to_persistable(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "channel": self.channel,
            "started_at": self.started_at,
            "args": self.args,
            "status": self.status,
            "finished_at": self.finished_at,
            "result": self.result,
            "error": self.error,
        }

    @classmethod
    def from_persistable(cls, data: dict[str, Any]) -> "Job":
        return cls(
            id=str(data["id"]),
            channel=str(data["channel"]),
            started_at=float(data["started_at"]),
            args=dict(data.get("args") or {}),
            status=str(data.get("status") or "orphaned"),
            finished_at=data.get("finished_at"),
            result=data.get("result"),
            error=data.get("error"),
            task=None,
        )


@dataclass
class Dispatcher:
    state_path: Path
    claude_bin: str = "claude"
    default_cwd: str | None = None
    max_completed_jobs: int = 1000
    log_path: Path | None = None
    persist_prompts: bool = False
    log_prompts: bool = False
    _channel_locks: dict[str, asyncio.Lock] = field(default_factory=dict, init=False)
    _state: dict[str, Any] = field(default_factory=dict, init=False)
    _jobs: dict[str, Job] = field(default_factory=dict, init=False)
    _jobs_save_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, init=False, repr=False
    )

    def __post_init__(self) -> None:
        self._state = self._load_state()
        self._jobs = self._load_jobs()
        self._mark_orphans_on_startup()

    # ----- state persistence -----

    def _load_state(self) -> dict[str, Any]:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                # Corrupt state shouldn't take down the bridge. Start fresh;
                # the worst case is one extra greeting in a channel.
                return {"channels": {}}
        return {"channels": {}}

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)

    # ----- job persistence -----

    @property
    def _jobs_path(self) -> Path:
        return self.state_path.parent / "jobs.json"

    def _load_jobs(self) -> dict[str, Job]:
        path = self._jobs_path
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Corrupt jobs file shouldn't take down the bridge — start
            # empty. Channel state in sessions.json is unaffected.
            return {}
        out: dict[str, Job] = {}
        for entry in data.get("jobs", []):
            try:
                job = Job.from_persistable(entry)
            except (KeyError, TypeError, ValueError):
                continue
            out[job.id] = job
        return out

    def _save_jobs_sync(self) -> None:
        """Same atomic temp+rename pattern as state. Safe to call before
        any asyncio loop is running (used during ``__post_init__``)."""
        path = self._jobs_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = {"jobs": [j.to_persistable() for j in self._jobs.values()]}
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    async def _save_jobs(self) -> None:
        """Async wrapper that serializes concurrent saves through a lock.

        Without the lock, two concurrent dispatch_async tasks could both
        write the temp file and rename in an order that loses one of
        their updates.
        """
        async with self._jobs_save_lock:
            self._save_jobs_sync()

    def _mark_orphans_on_startup(self) -> None:
        """Anything still in-flight at last write is an orphan now.

        We can't reattach to the asyncio.Task or the subprocess: those
        died with the previous bridge process. Two follow-on actions:

        1. Mark the job ``status="orphaned"`` so callers see a definite
           terminal state instead of "unknown".
        2. **Reset that job's channel pinning.** The orphaned ``claude
           -p`` may still be running in the container, attached to the
           same session id. If we leave the channel pinned and let a
           new dispatch ``--resume`` it, both processes race on the
           session log. Easier and safer: drop the pin so the next
           dispatch starts fresh.
        """
        channels_to_reset: set[str] = set()
        changed = False
        for job in self._jobs.values():
            if job.status in ("running", "starting"):
                job.status = "orphaned"
                if not job.error:
                    job.error = (
                        "bridge process restarted while the job was running; "
                        "result was not captured"
                    )
                if job.finished_at is None:
                    job.finished_at = time.time()
                channels_to_reset.add(job.channel)
                changed = True

        channels = self._state.setdefault("channels", {})
        state_changed = False
        for channel in channels_to_reset:
            if channel in channels:
                del channels[channel]
                state_changed = True

        if changed:
            self._save_jobs_sync()
        if state_changed:
            self._save_state()
        if changed or state_changed:
            self._log_event(
                "bridge_init_orphans",
                orphaned=len(channels_to_reset),
                channels_reset=sorted(channels_to_reset),
            )

    # ----- structured logging -----

    def _log_event(self, event: str, **fields: Any) -> None:
        """Append one JSONL event to ``log_path`` if configured.

        Failures are swallowed so a broken log path never takes down a
        dispatch. Prompts are excluded by default; opt in via
        ``log_prompts=True`` (or ``CLAUDE_BRIDGE_LOG_PROMPTS=1``).
        """
        if self.log_path is None:
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(
                {"ts": time.time(), "event": event, **fields},
                default=str,
            )
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    # ----- channel ops -----

    def list_channels(self) -> dict[str, str]:
        return dict(self._state.get("channels", {}))

    def reset_channel(self, channel: str) -> bool:
        channels = self._state.setdefault("channels", {})
        if channel in channels:
            del channels[channel]
            self._save_state()
            return True
        return False

    def _get_session(self, channel: str) -> str | None:
        return self._state.get("channels", {}).get(channel)

    def _set_session(self, channel: str, session_id: str) -> None:
        self._state.setdefault("channels", {})[channel] = session_id
        self._save_state()

    def _lock(self, channel: str) -> asyncio.Lock:
        lock = self._channel_locks.get(channel)
        if lock is None:
            lock = asyncio.Lock()
            self._channel_locks[channel] = lock
        return lock

    # ----- dispatch -----

    async def dispatch(
        self,
        prompt: str,
        channel: str = "default",
        timeout_seconds: int = 300,
        permission_mode: str = "acceptEdits",
        cwd: str | None = None,
    ) -> DispatchResult:
        if not prompt.strip():
            return DispatchResult(
                ok=False,
                channel=channel,
                duration_ms=0,
                error="prompt is empty",
            )

        async with self._lock(channel):
            session_id = self._get_session(channel)
            args = [
                self.claude_bin,
                "-p",
                prompt,
                "--output-format",
                "json",
                "--permission-mode",
                permission_mode,
            ]
            if session_id:
                args += ["--resume", session_id]
            else:
                # Pre-mint a session id so we can attribute the channel even
                # if claude exits before emitting JSON. claude -p accepts
                # --session-id <uuid> to bind a fresh session to that id.
                fresh = str(uuid.uuid4())
                args += ["--session-id", fresh]
                session_id = fresh

            start = time.monotonic()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd or self.default_cwd,
                )
            except FileNotFoundError:
                return DispatchResult(
                    ok=False,
                    channel=channel,
                    duration_ms=0,
                    error=f"{self.claude_bin!r} not on PATH inside the container",
                )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_seconds
                )
            except asyncio.TimeoutError:
                proc.kill()
                with contextlib.suppress(ProcessLookupError):
                    await proc.wait()
                return DispatchResult(
                    ok=False,
                    channel=channel,
                    duration_ms=int((time.monotonic() - start) * 1000),
                    error=f"timeout after {timeout_seconds}s",
                )
            except asyncio.CancelledError:
                # Caller cancelled (cancel_dispatch). Kill the subprocess so
                # we don't leave a claude -p running headlessly, then
                # propagate the cancellation.
                proc.kill()
                with contextlib.suppress(ProcessLookupError):
                    await proc.wait()
                raise

            duration_ms = int((time.monotonic() - start) * 1000)
            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")

            if proc.returncode != 0:
                return DispatchResult(
                    ok=False,
                    channel=channel,
                    duration_ms=duration_ms,
                    error=stderr.strip() or f"claude exited {proc.returncode}",
                    exit_code=proc.returncode,
                )

            try:
                payload = json.loads(stdout)
            except json.JSONDecodeError as exc:
                return DispatchResult(
                    ok=False,
                    channel=channel,
                    duration_ms=duration_ms,
                    error=f"could not parse claude JSON output: {exc}",
                )

            # Trust claude's reported session_id over our pre-minted one if
            # they differ (different claude versions handle --session-id
            # differently; the JSON is authoritative).
            reported = payload.get("session_id")
            if isinstance(reported, str) and reported:
                session_id = reported
            self._set_session(channel, session_id)

            return DispatchResult(
                ok=True,
                channel=channel,
                duration_ms=duration_ms,
                result=str(payload.get("result", "")),
                session_id=session_id,
                raw=payload if isinstance(payload, dict) else None,
                stderr=stderr.strip() or None,
            )

    # ----- async dispatch / polling -----

    def _is_terminal(self, job: Job) -> bool:
        """A job is terminal if its task is done OR if it has no live
        task and a non-running status (e.g. loaded from disk after a
        bridge restart, or marked orphaned)."""
        task = job.task
        if task is not None:
            return task.done()
        return job.status not in ("running", "starting")

    def _evict_completed_if_needed(self) -> None:
        """Bound the in-memory job table.

        Called *before* inserting a new job: if we're already at or above
        the cap, evict the oldest terminal entries to leave room for the
        incoming job. Running jobs are never evicted.
        """
        if len(self._jobs) < self.max_completed_jobs:
            return
        finished = sorted(
            (
                (jid, job)
                for jid, job in self._jobs.items()
                if self._is_terminal(job)
            ),
            key=lambda kv: kv[1].started_at,
        )
        to_drop = len(self._jobs) - self.max_completed_jobs + 1
        for jid, _ in finished[: max(to_drop, 0)]:
            self._jobs.pop(jid, None)

    async def dispatch_async(
        self,
        prompt: str,
        channel: str = "default",
        timeout_seconds: int = 300,
        permission_mode: str = "acceptEdits",
        cwd: str | None = None,
    ) -> str:
        """Kick off ``dispatch`` in the background; return a ``job_id``.

        Use this when the round trip might exceed the MCP transport's
        per-call ceiling (~60s). Channel locking still applies — concurrent
        async dispatches on the same channel queue up.

        Empty prompts raise ``ValueError`` here rather than scheduling a
        task that immediately errors out, so callers see the failure
        synchronously.
        """
        if not prompt.strip():
            raise ValueError("prompt is empty")
        self._evict_completed_if_needed()
        job_id = str(uuid.uuid4())
        args = {
            "channel": channel,
            "timeout_seconds": timeout_seconds,
            "permission_mode": permission_mode,
            "cwd": cwd,
        }
        if self.persist_prompts:
            args["prompt"] = prompt
        job = Job(
            id=job_id,
            channel=channel,
            started_at=time.time(),
            args=args,
            status="running",
        )
        self._jobs[job_id] = job
        await self._save_jobs()
        log_fields: dict[str, Any] = {
            "job_id": job_id,
            "channel": channel,
            "cwd": cwd,
            "permission_mode": permission_mode,
            "timeout_seconds": timeout_seconds,
        }
        if self.log_prompts:
            log_fields["prompt"] = prompt
        self._log_event("dispatch_start", **log_fields)

        coro = self._tracked_dispatch(
            job,
            prompt=prompt,
            channel=channel,
            timeout_seconds=timeout_seconds,
            permission_mode=permission_mode,
            cwd=cwd,
        )
        job.task = asyncio.create_task(coro, name=f"dispatch:{job_id}")
        return job_id

    async def _tracked_dispatch(
        self, job: Job, **dispatch_kwargs: Any
    ) -> DispatchResult:
        """Run ``dispatch`` and persist the job's terminal state.

        Save-on-completion is what makes the bridge trustworthy across
        restarts: even if the bridge crashes immediately after this
        write, ``get_dispatch(job_id)`` after restart still returns the
        recorded result.
        """
        try:
            result = await self.dispatch(**dispatch_kwargs)
        except asyncio.CancelledError:
            job.status = "cancelled"
            job.finished_at = time.time()
            job.error = "cancelled by caller"
            await self._save_jobs()
            self._log_event("dispatch_cancelled", job_id=job.id, channel=job.channel)
            raise
        except Exception as exc:
            job.status = "error"
            job.error = f"{type(exc).__name__}: {exc}"
            job.finished_at = time.time()
            await self._save_jobs()
            self._log_event(
                "dispatch_error",
                job_id=job.id,
                channel=job.channel,
                error=job.error,
            )
            raise

        job.status = "done"
        job.finished_at = time.time()
        job.result = result.to_dict()
        await self._save_jobs()
        self._log_event(
            "dispatch_end",
            job_id=job.id,
            channel=job.channel,
            ok=result.ok,
            duration_ms=result.duration_ms,
            exit_code=result.exit_code,
        )
        return result

    def _live_status(self, job: Job) -> dict[str, Any]:
        """Read status from the in-memory ``asyncio.Task``."""
        base = {
            "job_id": job.id,
            "channel": job.channel,
            "started_at": job.started_at,
            "args": job.args,
        }
        task = job.task
        assert task is not None, "_live_status called without a task"
        if not task.done():
            return {
                **base,
                "status": "running",
                "elapsed_ms": int((time.time() - job.started_at) * 1000),
            }
        if task.cancelled():
            return {**base, "status": "cancelled", "ok": False}
        exc = task.exception()
        if exc is not None:
            return {
                **base,
                "status": "error",
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        result = task.result()
        return {**base, "status": "done", **result.to_dict()}

    def _persisted_status(self, job: Job) -> dict[str, Any]:
        """Read status from the persisted record (job has no live task)."""
        base = {
            "job_id": job.id,
            "channel": job.channel,
            "started_at": job.started_at,
            "args": job.args,
            "finished_at": job.finished_at,
        }
        if job.status == "done" and job.result is not None:
            return {**base, "status": "done", **job.result}
        return {
            **base,
            "status": job.status,
            "ok": False,
            "error": job.error or f"job ended with status={job.status!r}",
        }

    def get_dispatch(self, job_id: str) -> dict[str, Any]:
        """Return the current state of a job. Non-blocking.

        Works for both live jobs (with an in-memory ``asyncio.Task``) and
        ghost jobs reloaded from disk after a bridge restart. Status
        values: ``running``, ``done``, ``cancelled``, ``error``,
        ``orphaned``. ``orphaned`` means the bridge restarted while this
        job was still in flight; the underlying claude subprocess may
        still be running but its result was not captured.
        """
        job = self._jobs.get(job_id)
        if job is None:
            return {"ok": False, "error": f"unknown job_id: {job_id}"}
        if job.task is None:
            return self._persisted_status(job)
        return self._live_status(job)

    async def wait_dispatch(
        self, job_id: str, max_wait_seconds: float = 50.0
    ) -> dict[str, Any]:
        """Block up to ``max_wait_seconds`` for a job, then return status.

        Default 50s is intentionally below the typical MCP transport
        ceiling (~60s) — Cowork can poll this in a loop until status is
        no longer ``running``. ``asyncio.shield`` keeps this poller from
        cancelling the underlying job if its own MCP call gets aborted.
        Ghost jobs (no live task) return immediately with their persisted
        state.
        """
        job = self._jobs.get(job_id)
        if job is None:
            return {"ok": False, "error": f"unknown job_id: {job_id}"}
        task = job.task
        if task is None:
            return self._persisted_status(job)
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=max_wait_seconds)
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            # The shield re-raises if the inner task ended (e.g. via
            # cancel_dispatch). If the inner task is still alive, then
            # *our* coroutine got cancelled and we must propagate.
            if not task.done():
                raise
        return self.get_dispatch(job_id)

    def cancel_dispatch(self, job_id: str) -> dict[str, Any]:
        """Request cancellation of a running job.

        Returns ``{"cancelled": true}`` if the cancel was requested. The
        task's ``CancelledError`` handler kills the underlying subprocess
        before re-raising, so we don't leave orphan workers.
        ``{"cancelled": false}`` for jobs that have already finished or
        are ghosts (no live task).
        """
        job = self._jobs.get(job_id)
        if job is None:
            return {"ok": False, "error": f"unknown job_id: {job_id}"}
        base = {"job_id": job.id, "channel": job.channel}
        task = job.task
        if task is None:
            return {
                **base,
                "cancelled": False,
                "reason": "no_live_task",
                "status": job.status,
            }
        if task.done():
            return {**base, "cancelled": False, "reason": "already_finished"}
        task.cancel()
        return {**base, "cancelled": True}

    def list_jobs(self) -> list[dict[str, Any]]:
        """Return one summary dict per tracked job (running + finished).

        ``raw`` is stripped from done-state summaries to keep this cheap
        on long-lived bridges; call ``get_dispatch(job_id)`` for the full
        payload.
        """
        out = []
        for jid in self._jobs:
            entry = self.get_dispatch(jid)
            if entry.get("status") == "done":
                entry = {k: v for k, v in entry.items() if k != "raw"}
            out.append(entry)
        return out
