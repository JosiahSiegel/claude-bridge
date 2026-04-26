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
        return out


@dataclass
class Job:
    """Background dispatch job tracked by ``Dispatcher``.

    A ``Job`` wraps the ``asyncio.Task`` running the underlying ``dispatch``
    so callers can poll status, retrieve the result once finished, or
    cancel it. The task itself returns a ``DispatchResult`` (sync-style
    failures) or raises (cancellation, programmer error).
    """

    id: str
    channel: str
    started_at: float
    task: "asyncio.Task[DispatchResult]" = field(repr=False)


@dataclass
class Dispatcher:
    state_path: Path
    claude_bin: str = "claude"
    default_cwd: str | None = None
    max_completed_jobs: int = 1000
    _channel_locks: dict[str, asyncio.Lock] = field(default_factory=dict, init=False)
    _state: dict[str, Any] = field(default_factory=dict, init=False)
    _jobs: dict[str, Job] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._state = self._load_state()

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
            )

    # ----- async dispatch / polling -----

    def _evict_completed_if_needed(self) -> None:
        """Bound the in-memory job table.

        Called *before* inserting a new job: if we're already at or above
        the cap, evict the oldest finished entries to leave room for the
        incoming job. Currently running jobs are never evicted.
        """
        if len(self._jobs) < self.max_completed_jobs:
            return
        finished = sorted(
            (
                (jid, job)
                for jid, job in self._jobs.items()
                if job.task.done()
            ),
            key=lambda kv: kv[1].started_at,
        )
        # Need to free at least one slot for the incoming job.
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
        coro = self.dispatch(
            prompt=prompt,
            channel=channel,
            timeout_seconds=timeout_seconds,
            permission_mode=permission_mode,
            cwd=cwd,
        )
        task = asyncio.create_task(coro, name=f"dispatch:{job_id}")
        self._jobs[job_id] = Job(
            id=job_id,
            channel=channel,
            started_at=time.time(),
            task=task,
        )
        return job_id

    def get_dispatch(self, job_id: str) -> dict[str, Any]:
        """Return the current state of a job. Non-blocking.

        Status values: ``running`` (task not yet done), ``done`` (task
        finished and returned a ``DispatchResult``), ``cancelled`` (task
        was cancelled), ``error`` (task raised something other than
        ``CancelledError`` — this is a programmer error in the dispatcher,
        not a normal claude failure).

        For ``done``, the returned dict carries the same keys as
        ``DispatchResult.to_dict()`` plus ``status``, ``job_id``,
        ``started_at``.
        """
        job = self._jobs.get(job_id)
        if job is None:
            return {"ok": False, "error": f"unknown job_id: {job_id}"}

        base = {
            "job_id": job.id,
            "channel": job.channel,
            "started_at": job.started_at,
        }

        if not job.task.done():
            return {
                **base,
                "status": "running",
                "elapsed_ms": int((time.time() - job.started_at) * 1000),
            }

        if job.task.cancelled():
            return {**base, "status": "cancelled", "ok": False}

        exc = job.task.exception()
        if exc is not None:
            return {
                **base,
                "status": "error",
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

        result = job.task.result()
        return {**base, "status": "done", **result.to_dict()}

    async def wait_dispatch(
        self, job_id: str, max_wait_seconds: float = 50.0
    ) -> dict[str, Any]:
        """Block up to ``max_wait_seconds`` for a job, then return status.

        Default 50s is intentionally below the typical MCP transport
        ceiling (~60s) — Cowork can poll this in a loop until status is
        no longer ``running``. ``asyncio.shield`` keeps this poller from
        cancelling the underlying job if its own MCP call gets aborted.
        """
        job = self._jobs.get(job_id)
        if job is None:
            return {"ok": False, "error": f"unknown job_id: {job_id}"}
        try:
            await asyncio.wait_for(
                asyncio.shield(job.task), timeout=max_wait_seconds
            )
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            # Two paths can land here:
            #   1. The inner job was cancelled (e.g. via cancel_dispatch) —
            #      the shield re-raises so we return its now-final state.
            #   2. Our own coroutine was cancelled (e.g. the MCP request was
            #      aborted) — the inner task survives because of the shield,
            #      and we must propagate so the asyncio runtime sees the
            #      cancellation.
            if not job.task.done():
                raise
        return self.get_dispatch(job_id)

    def cancel_dispatch(self, job_id: str) -> dict[str, Any]:
        """Request cancellation of a running job.

        Returns ``{"cancelled": true}`` if the cancel was requested (the
        task may take a tick to wind down — its ``CancelledError`` handler
        kills the subprocess before re-raising). Returns
        ``{"cancelled": false}`` if the job already finished.
        """
        job = self._jobs.get(job_id)
        if job is None:
            return {"ok": False, "error": f"unknown job_id: {job_id}"}
        if job.task.done():
            return {
                "cancelled": False,
                "job_id": job.id,
                "channel": job.channel,
                "reason": "already_finished",
            }
        job.task.cancel()
        return {"cancelled": True, "job_id": job.id, "channel": job.channel}

    def list_jobs(self) -> list[dict[str, Any]]:
        """Return one summary dict per tracked job (running + finished)."""
        return [self.get_dispatch(jid) for jid in self._jobs]
