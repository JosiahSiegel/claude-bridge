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
  surfaces them to Cowork as tool errors.

* **Subprocess decoupling for async dispatches.** The async path
  (``dispatch_async`` → ``_tracked_dispatch``) spawns ``claude -p`` in
  its own session (``start_new_session=True``) with stdout/stderr
  redirected to files in ``<state_path>.parent / "job-output" / <job_id>``.
  Three things follow:

  1. The subprocess is **independent of the asyncio task** that's
     waiting on it. If FastMCP cancels the dispatch task (e.g. because
     of a transport timeout), the subprocess keeps running and writing
     to its files — the "transport timeout treated as cancel" bug.
  2. The subprocess is **independent of the bridge process**. If the
     bridge crashes or is restarted by Claude Desktop, the subprocess
     keeps running, parented to PID 1.
  3. **Output is durably captured on disk**, not held in memory pipes.
     A new bridge can recover the result of a subprocess that finished
     while the bridge was down.

  Sync ``dispatch`` (the ``dispatch`` MCP tool) still uses pipes — it's
  for short calls under the MCP ceiling and doesn't need the
  ceremony.

* **Two flavors of cancellation.** ``cancel_dispatch`` is the *user*
  asking us to stop; we set ``Job.cancel_requested = True``, SIGTERM
  the PID, and let the asyncio task wind down. Status becomes
  ``cancelled``. Anything else that cancels the task (FastMCP shutting
  it down, the loop tearing down) is a *runtime* cancellation; the
  subprocess is left running and the job is marked ``abandoned`` —
  the bridge will pick it up on the next watcher tick and finalize
  it from its output files.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as _dt
import json
import signal
import urllib.request
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


STOP_SENTINEL = "[BRIDGE_STOP_SCHEDULE]"
"""Magic string a scheduled tick can emit in its result to cancel its own
schedule. Useful for "watch X until Y" patterns where the prompt knows
the termination condition but the caller doesn't."""


@dataclass
class Schedule:
    """A recurring dispatch fired by the bridge's scheduler loop.

    Each tick runs as an independent ``dispatch_async`` job — short
    individual runs that sidestep the MCP transport's per-call ceiling
    while still adding up to long-running observation work.

    Schedules persist across bridge restarts. ``last_tick_at`` is used
    on restart to decide if the next tick is overdue (fire once, don't
    catch up missed ticks).
    """

    id: str
    prompt: str
    channel: str
    interval_seconds: float
    until: float | None  # epoch seconds, None = run forever
    args: dict[str, Any]
    created_at: float
    last_tick_at: float | None = None
    last_job_id: str | None = None
    last_tick_status: str | None = None
    tick_count: int = 0
    status: str = "active"  # waiting | active | completed | cancelled | error
    error: str | None = None
    after_schedule_id: str | None = None
    notify_url: str | None = None
    notify_on: list[str] = field(default_factory=list)
    notify_headers: dict[str, str] = field(default_factory=dict)

    def to_persistable(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "channel": self.channel,
            "interval_seconds": self.interval_seconds,
            "until": self.until,
            "args": self.args,
            "created_at": self.created_at,
            "last_tick_at": self.last_tick_at,
            "last_job_id": self.last_job_id,
            "last_tick_status": self.last_tick_status,
            "tick_count": self.tick_count,
            "status": self.status,
            "error": self.error,
            "after_schedule_id": self.after_schedule_id,
            "notify_url": self.notify_url,
            "notify_on": list(self.notify_on),
            "notify_headers": dict(self.notify_headers),
        }

    @classmethod
    def from_persistable(cls, data: dict[str, Any]) -> "Schedule":
        return cls(
            id=str(data["id"]),
            prompt=str(data.get("prompt", "")),
            channel=str(data["channel"]),
            interval_seconds=float(data["interval_seconds"]),
            until=data.get("until"),
            args=dict(data.get("args") or {}),
            created_at=float(data.get("created_at") or time.time()),
            last_tick_at=data.get("last_tick_at"),
            last_job_id=data.get("last_job_id"),
            last_tick_status=data.get("last_tick_status"),
            tick_count=int(data.get("tick_count") or 0),
            status=str(data.get("status") or "active"),
            error=data.get("error"),
            after_schedule_id=data.get("after_schedule_id"),
            notify_url=data.get("notify_url"),
            notify_on=list(data.get("notify_on") or []),
            notify_headers=dict(data.get("notify_headers") or {}),
        )

    def public_view(self) -> dict[str, Any]:
        """The shape returned by ``get_schedule`` / ``list_schedules``."""
        return self.to_persistable()


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
    pid: int | None = None
    output_dir: str | None = None
    notify_url: str | None = None
    notify_on: list[str] = field(default_factory=list)
    notify_headers: dict[str, str] = field(default_factory=dict)
    task: "asyncio.Task[DispatchResult] | None" = field(default=None, repr=False)
    cancel_requested: bool = field(default=False, repr=False)

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
            "pid": self.pid,
            "output_dir": self.output_dir,
            "notify_url": self.notify_url,
            "notify_on": list(self.notify_on),
            "notify_headers": dict(self.notify_headers),
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
            pid=data.get("pid"),
            output_dir=data.get("output_dir"),
            notify_url=data.get("notify_url"),
            notify_on=list(data.get("notify_on") or []),
            notify_headers=dict(data.get("notify_headers") or {}),
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
    max_events: int = 1000
    _watcher_poll_seconds: float = field(default=2.0, repr=False)
    _scheduler_poll_seconds: float = field(default=5.0, repr=False)
    _min_schedule_interval_seconds: float = field(default=10.0, repr=False)
    _channel_locks: dict[str, asyncio.Lock] = field(default_factory=dict, init=False)
    _watcher_tasks: dict[str, "asyncio.Task[None]"] = field(
        default_factory=dict, init=False, repr=False
    )
    _schedules: dict[str, Schedule] = field(default_factory=dict, init=False)
    _scheduler_task: "asyncio.Task[None] | None" = field(
        default=None, init=False, repr=False
    )
    _schedules_save_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, init=False, repr=False
    )
    _events: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)
    _state: dict[str, Any] = field(default_factory=dict, init=False)
    _jobs: dict[str, Job] = field(default_factory=dict, init=False)
    _jobs_save_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, init=False, repr=False
    )

    def __post_init__(self) -> None:
        self._state = self._load_state()
        self._jobs = self._load_jobs()
        self._mark_orphans_on_startup()
        self._schedules = self._load_schedules()
        self._events = self._load_events()

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
        """Recover jobs that were in-flight at last write.

        Three cases for each ``running`` job:

        1. **Subprocess still alive** (PID exists, output dir present).
           Leave the job as ``running``; spawn a watcher (via
           ``ensure_watchers_running``, which the server calls in its
           startup hook) that will reap it when it exits.
        2. **Subprocess already exited** (PID gone, output dir present).
           Read the output files and finalize the result. Cowork's
           ``get_dispatch`` after restart returns a real ``done`` (or
           ``error``) result — not an orphan tombstone.
        3. **Subprocess never started or output is gone** (no PID, no
           output dir, or output dir missing). Mark ``orphaned`` with a
           clear error.

        For (1) and (2) we **do not** reset the channel pinning — the
        subprocess we're tracking either still has the right session
        id, or was the only writer to it. For (3) we reset, because
        we've fully lost track and don't want a new dispatch to race
        with a stray ``claude -p``.
        """
        running_to_finalize: list[Job] = []
        channels_to_reset: set[str] = set()
        changed = False

        for job in self._jobs.values():
            if job.status not in ("running", "starting"):
                continue
            # Case (1) and (2): we have a PID and output dir.
            if (
                job.pid is not None
                and job.output_dir
                and Path(job.output_dir).exists()
            ):
                if self._is_pid_alive(job.pid):
                    # Watcher will be spawned by ensure_watchers_running.
                    self._log_event(
                        "bridge_init_subprocess_alive",
                        job_id=job.id,
                        channel=job.channel,
                        pid=job.pid,
                    )
                    continue
                # Already exited; finalize synchronously here.
                running_to_finalize.append(job)
                continue

            # Case (3): we've lost track entirely.
            job.status = "orphaned"
            if not job.error:
                job.error = (
                    "bridge process restarted while the job was running; "
                    "subprocess could not be located on disk and result "
                    "was not captured"
                )
            if job.finished_at is None:
                job.finished_at = time.time()
            channels_to_reset.add(job.channel)
            changed = True

        # Inline-finalize the ones we know exited cleanly. We can't
        # await here (we're in __post_init__, no event loop), so we
        # do the synchronous read + parse without _save_jobs's lock.
        for job in running_to_finalize:
            self._finalize_orphan_sync(job)
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
        if changed or state_changed or running_to_finalize:
            self._log_event(
                "bridge_init_recovery",
                orphaned=len(channels_to_reset),
                finalized=len(running_to_finalize),
                channels_reset=sorted(channels_to_reset),
            )

    def _finalize_orphan_sync(self, job: Job) -> None:
        """Synchronous variant of ``_finalize_orphan`` for use in
        ``__post_init__`` where there's no running event loop yet."""
        out_dir = Path(job.output_dir) if job.output_dir else None
        if out_dir is None or not out_dir.exists():
            job.status = "orphaned"
            if not job.error:
                job.error = "subprocess output directory missing on recovery"
            job.finished_at = job.finished_at or time.time()
            return

        stdout = ""
        stderr = ""
        with contextlib.suppress(OSError):
            stdout = (out_dir / "stdout").read_text(encoding="utf-8", errors="replace")
        with contextlib.suppress(OSError):
            stderr = (out_dir / "stderr").read_text(encoding="utf-8", errors="replace")
        session_id = job.args.get("session_id") or ""
        duration_ms = int(((job.finished_at or time.time()) - job.started_at) * 1000)
        result = self._finalize_payload(
            channel=job.channel,
            session_id=str(session_id),
            duration_ms=duration_ms,
            returncode=0 if stdout.strip() else 1,
            stdout=stdout,
            stderr=stderr,
        )
        job.status = "done" if result.ok else "error"
        job.result = result.to_dict()
        job.error = None if result.ok else result.error
        job.finished_at = time.time()

    # ----- schedule persistence -----

    @property
    def _schedules_path(self) -> Path:
        return self.state_path.parent / "schedules.json"

    def _load_schedules(self) -> dict[str, Schedule]:
        path = self._schedules_path
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        out: dict[str, Schedule] = {}
        for entry in data.get("schedules", []):
            try:
                sched = Schedule.from_persistable(entry)
            except (KeyError, TypeError, ValueError):
                continue
            out[sched.id] = sched
        return out

    def _save_schedules_sync(self) -> None:
        path = self._schedules_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = {
            "schedules": [s.to_persistable() for s in self._schedules.values()]
        }
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    async def _save_schedules(self) -> None:
        async with self._schedules_save_lock:
            self._save_schedules_sync()

    # ----- structured event log -----
    #
    # Two surfaces:
    #
    # 1. The optional file log at ``log_path`` (set via
    #    ``CLAUDE_BRIDGE_LOG``) — append-only JSONL, the durable record
    #    for ops/post-mortem.
    # 2. The in-memory ring buffer ``_events`` plus its disk mirror at
    #    ``events.json`` — bounded, queryable via ``list_events``,
    #    persisted across bridge restarts. This is the surface Cowork
    #    uses for "what happened while I was offline?".
    #
    # Both are populated from the single ``_log_event`` call site, so a
    # new event type added anywhere flows to both surfaces automatically.

    @property
    def _events_path(self) -> Path:
        return self.state_path.parent / "events.json"

    def _load_events(self) -> list[dict[str, Any]]:
        path = self._events_path
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        raw = data.get("events", [])
        if not isinstance(raw, list):
            return []
        return list(raw)[-self.max_events :]

    def _save_events_sync(self) -> None:
        path = self._events_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = {"events": self._events}
        tmp.write_text(json.dumps(payload, default=str), encoding="utf-8")
        os.replace(tmp, path)

    def _log_event(self, event: str, **fields: Any) -> None:
        """Append a structured event. Hits the in-memory buffer plus the
        on-disk JSON mirror, plus the optional ``log_path`` JSONL file.

        Failures on either persistence path are swallowed so a broken
        log path never takes down a dispatch. Prompts are excluded by
        default; opt in via ``log_prompts=True`` (or
        ``CLAUDE_BRIDGE_LOG_PROMPTS=1``).
        """
        record: dict[str, Any] = {
            "ts": time.time(),
            "event": event,
            **fields,
        }
        # In-memory buffer + persisted mirror (always on; bounded).
        self._events.append(record)
        if len(self._events) > self.max_events:
            self._events = self._events[-self.max_events :]
        try:
            self._save_events_sync()
        except OSError:
            pass

        # Optional human-readable JSONL file.
        if self.log_path is None:
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(record, default=str)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def list_events(
        self,
        since: float = 0.0,
        limit: int = 100,
        types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return events whose ``ts > since``, oldest first, capped at
        ``limit``. Optional ``types`` filters by event name."""
        out: list[dict[str, Any]] = []
        types_set = set(types) if types else None
        for record in self._events:
            ts = record.get("ts")
            if not isinstance(ts, (int, float)) or ts <= since:
                continue
            if types_set is not None and record.get("event") not in types_set:
                continue
            out.append(record)
            if limit > 0 and len(out) >= limit:
                break
        return out

    # ----- outbound webhooks (best-effort push) -----
    #
    # Both Job and Schedule support optional ``notify_url`` /
    # ``notify_on`` / ``notify_headers``. When a hooked event fires we
    # POST a small JSON payload to ``notify_url`` and log the outcome
    # (``webhook_sent`` / ``webhook_failed``) to the event log.
    #
    # Delivery is fire-and-log: no retries, no queueing, no caller
    # propagation. The bridge is intentionally not a delivery system.
    # The destination is expected to be a small relay the user owns
    # (e.g. an HTTP endpoint that forwards to Slack/Pushover/email or
    # injects a chat message). All real durability — "did the user
    # actually see it?" — lives in that relay.

    _WEBHOOK_MAX_RESULT_CHARS = 4096
    _WEBHOOK_TIMEOUT_SECONDS = 10.0

    @staticmethod
    def _truncate_for_webhook(s: str | None) -> str | None:
        if s is None:
            return None
        if len(s) <= Dispatcher._WEBHOOK_MAX_RESULT_CHARS:
            return s
        cap = Dispatcher._WEBHOOK_MAX_RESULT_CHARS
        return s[:cap] + f"…[truncated, original {len(s)} chars]"

    async def _send_webhook(
        self, url: str, headers: dict[str, str] | None, payload: dict[str, Any]
    ) -> None:
        """Fire-and-log POST. Errors are swallowed and logged as
        ``webhook_failed``; never propagated to the caller."""
        body = json.dumps(payload, default=str).encode("utf-8")
        req_headers = {
            "Content-Type": "application/json",
            "User-Agent": "claude-bridge/0.1",
            **(headers or {}),
        }
        req = urllib.request.Request(
            url, data=body, headers=req_headers, method="POST"
        )

        def _send() -> int:
            with urllib.request.urlopen(
                req, timeout=self._WEBHOOK_TIMEOUT_SECONDS
            ) as resp:
                return int(resp.status)

        loop = asyncio.get_running_loop()
        event = payload.get("event")
        try:
            status = await loop.run_in_executor(None, _send)
            self._log_event(
                "webhook_sent", url=url, payload_event=event, http_status=status
            )
        except Exception as exc:  # noqa: BLE001
            self._log_event(
                "webhook_failed",
                url=url,
                payload_event=event,
                error=f"{type(exc).__name__}: {exc}",
            )

    async def _maybe_notify_job(self, job: Job, event: str) -> None:
        """If the job opted into webhooks for this event, send it."""
        if not job.notify_url:
            return
        configured = job.notify_on or ["done"]
        if event not in configured:
            return
        result_preview = None
        ok = None
        if job.result is not None:
            ok = job.result.get("ok")
            result_preview = self._truncate_for_webhook(
                str(job.result.get("result") or "")
            )
        payload = {
            "event": event,
            "job_id": job.id,
            "channel": job.channel,
            "status": job.status,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "ok": ok,
            "result_preview": result_preview,
            "error": job.error,
        }
        await self._send_webhook(job.notify_url, job.notify_headers, payload)

    async def _maybe_notify_schedule(
        self,
        schedule: Schedule,
        event: str,
        last_tick_result: str | None = None,
    ) -> None:
        if not schedule.notify_url:
            return
        configured = schedule.notify_on or ["schedule_end"]
        if event not in configured:
            return
        payload = {
            "event": event,
            "schedule_id": schedule.id,
            "channel": schedule.channel,
            "tick_count": schedule.tick_count,
            "status": schedule.status,
            "last_tick_at": schedule.last_tick_at,
            "last_tick_result": self._truncate_for_webhook(last_tick_result),
            "last_job_id": schedule.last_job_id,
            "error": schedule.error,
        }
        await self._send_webhook(
            schedule.notify_url, schedule.notify_headers, payload
        )

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

    @property
    def _jobs_output_dir(self) -> Path:
        return self.state_path.parent / "job-output"

    def _build_args(
        self, prompt: str, permission_mode: str, channel: str
    ) -> tuple[list[str], str]:
        """Construct the ``claude -p`` argv plus the session id this call
        will operate on (pre-minted for fresh channels, looked up otherwise)."""
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
            fresh = str(uuid.uuid4())
            args += ["--session-id", fresh]
            session_id = fresh
        return args, session_id

    def _finalize_payload(
        self,
        channel: str,
        session_id: str,
        duration_ms: int,
        returncode: int | None,
        stdout: str,
        stderr: str,
    ) -> DispatchResult:
        """Turn a finished subprocess's output into a ``DispatchResult``.

        Shared between the sync (pipe) and async (file) paths and the
        recovery path (reading output files of an orphaned subprocess).
        """
        if returncode != 0:
            return DispatchResult(
                ok=False,
                channel=channel,
                duration_ms=duration_ms,
                error=stderr.strip() or f"claude exited {returncode}",
                exit_code=returncode,
                stderr=stderr.strip() or None,
            )
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            return DispatchResult(
                ok=False,
                channel=channel,
                duration_ms=duration_ms,
                error=f"could not parse claude JSON output: {exc}",
                stderr=stderr.strip() or None,
            )
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

    async def dispatch(
        self,
        prompt: str,
        channel: str = "default",
        timeout_seconds: int = 300,
        permission_mode: str = "acceptEdits",
        cwd: str | None = None,
        job: Job | None = None,
    ) -> DispatchResult:
        """Run ``claude -p`` and return a structured result.

        Two modes:

        * **Pipe mode** (``job=None``, used by the synchronous ``dispatch``
          MCP tool). Subprocess inherits the bridge's process group;
          stdout/stderr are pipes drained by ``proc.communicate()``. If
          this coroutine is cancelled, the subprocess is killed.

        * **File mode** (``job`` is provided, used by ``_tracked_dispatch``
          for ``dispatch_async``). Subprocess is spawned with
          ``start_new_session=True`` (its own session, won't get SIGHUP
          when the bridge exits) and stdout/stderr are redirected to
          files under ``<state>.parent / "job-output" / <job_id>``. If
          this coroutine is cancelled, the subprocess keeps running.
          The PID and output paths are persisted so a new bridge can
          recover the result on restart.
        """
        if not prompt.strip():
            return DispatchResult(
                ok=False,
                channel=channel,
                duration_ms=0,
                error="prompt is empty",
            )

        async with self._lock(channel):
            args, session_id = self._build_args(prompt, permission_mode, channel)
            if job is not None:
                return await self._dispatch_to_files(
                    args=args,
                    channel=channel,
                    session_id=session_id,
                    timeout_seconds=timeout_seconds,
                    cwd=cwd,
                    job=job,
                )
            return await self._dispatch_to_pipes(
                args=args,
                channel=channel,
                session_id=session_id,
                timeout_seconds=timeout_seconds,
                cwd=cwd,
            )

    async def _dispatch_to_pipes(
        self,
        *,
        args: list[str],
        channel: str,
        session_id: str,
        timeout_seconds: int,
        cwd: str | None,
    ) -> DispatchResult:
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
            # Sync dispatch: caller cancelled. Kill the subprocess so we
            # don't leave a headless claude running, then propagate.
            proc.kill()
            with contextlib.suppress(ProcessLookupError):
                await proc.wait()
            raise

        duration_ms = int((time.monotonic() - start) * 1000)
        return self._finalize_payload(
            channel=channel,
            session_id=session_id,
            duration_ms=duration_ms,
            returncode=proc.returncode,
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
        )

    async def _dispatch_to_files(
        self,
        *,
        args: list[str],
        channel: str,
        session_id: str,
        timeout_seconds: int,
        cwd: str | None,
        job: Job,
    ) -> DispatchResult:
        out_dir = self._jobs_output_dir / job.id
        out_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = out_dir / "stdout"
        stderr_path = out_dir / "stderr"
        # Open in 'wb' so any prior partial output (from a recovery
        # scenario) gets clobbered cleanly.
        stdout_f = stdout_path.open("wb")
        stderr_f = stderr_path.open("wb")

        start = time.monotonic()
        try:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=stdout_f,
                    stderr=stderr_f,
                    cwd=cwd or self.default_cwd,
                    start_new_session=True,
                )
            except FileNotFoundError:
                return DispatchResult(
                    ok=False,
                    channel=channel,
                    duration_ms=0,
                    error=f"{self.claude_bin!r} not on PATH inside the container",
                )

            # Persist the PID and output dir BEFORE awaiting completion.
            # If we crash here, recovery on restart will find the running
            # subprocess and pick up where we left off.
            job.pid = proc.pid
            job.output_dir = str(out_dir)
            job.args["session_id"] = session_id
            await self._save_jobs()

            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout_seconds)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                with contextlib.suppress(ProcessLookupError):
                    await proc.wait()
                return DispatchResult(
                    ok=False,
                    channel=channel,
                    duration_ms=int((time.monotonic() - start) * 1000),
                    error=f"timeout after {timeout_seconds}s",
                )
            # Note: we deliberately do NOT catch CancelledError here.
            # If the asyncio task is cancelled, the subprocess keeps
            # running (its own session) and writes to the output files.
            # _tracked_dispatch's CancelledError handler decides whether
            # the cancel was user-requested (cancel_dispatch — kill the
            # subprocess) or runtime-induced (mark abandoned, leave it
            # running for the watcher to pick up).
        finally:
            stdout_f.close()
            stderr_f.close()

        duration_ms = int((time.monotonic() - start) * 1000)
        return self._finalize_payload(
            channel=channel,
            session_id=session_id,
            duration_ms=duration_ms,
            returncode=proc.returncode,
            stdout=stdout_path.read_text(encoding="utf-8", errors="replace"),
            stderr=stderr_path.read_text(encoding="utf-8", errors="replace"),
        )

    # ----- subprocess liveness / orphan reaping -----

    @staticmethod
    def _is_pid_alive(pid: int) -> bool:
        """Best-effort liveness check that doesn't require us to be the parent.

        ``os.kill(pid, 0)`` returns success for both running and zombie
        processes; ``/proc/<pid>/status`` lets us filter zombies out.
        Any failure mode (no /proc, no permission, etc.) defers to
        ``os.kill`` which is portable enough.
        """
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists but we can't signal it
        try:
            with open(f"/proc/{pid}/status", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("State:"):
                        state = line.split(maxsplit=2)[1]
                        return state not in ("Z", "X")
        except OSError:
            pass
        return True

    def _spawn_watcher(self, job: Job) -> None:
        """Schedule a background coroutine that polls ``job.pid`` until
        it exits, then finalizes the job from its on-disk output files.

        Idempotent — if a watcher is already running for this job, skip.
        Used in three situations:

        * ``_tracked_dispatch`` was cancelled by the runtime (not the
          user). The subprocess is still running; the watcher will
          finalize it when it exits.
        * Bridge startup found a job persisted in the ``running`` state
          with a live PID (``_mark_orphans_on_startup``).
        * First async tool call after a restart bootstraps watchers
          via ``ensure_watchers_running``.
        """
        if job.id in self._watcher_tasks and not self._watcher_tasks[job.id].done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No event loop yet (e.g. construction during a sync test).
            # The watcher will be spawned by ``ensure_watchers_running``
            # the first time anything async is called.
            return
        task = loop.create_task(self._watcher(job), name=f"watch:{job.id}")
        self._watcher_tasks[job.id] = task

    async def _watcher(self, job: Job) -> None:
        """Poll the job's PID until it exits, then finalize from output."""
        if job.pid is None or job.output_dir is None:
            # Nothing to watch — fall through to mark the job orphaned.
            await self._finalize_orphan(job)
            return
        try:
            while self._is_pid_alive(job.pid):
                if job.cancel_requested:
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.killpg(os.getpgid(job.pid), signal.SIGKILL)
                await asyncio.sleep(self._watcher_poll_seconds)
        except asyncio.CancelledError:
            # Watcher itself cancelled (e.g. bridge shutdown). Leave the
            # subprocess; the next bridge will pick it up.
            return
        await self._finalize_orphan(job)

    async def _finalize_orphan(self, job: Job) -> None:
        """Read the orphaned job's output files and produce a final result.

        We don't have the subprocess's exit code (we weren't its parent
        when it exited), so we treat the JSON output as authoritative.
        Well-formed JSON → success; malformed or missing → error.
        """
        out_dir = Path(job.output_dir) if job.output_dir else None
        if out_dir is None or not out_dir.exists():
            job.status = "orphaned"
            if not job.error:
                job.error = "subprocess output directory missing on recovery"
            job.finished_at = job.finished_at or time.time()
            await self._save_jobs()
            self._log_event(
                "dispatch_orphan_finalized_missing",
                job_id=job.id,
                channel=job.channel,
            )
            return

        stdout = ""
        stderr = ""
        with contextlib.suppress(OSError):
            stdout = (out_dir / "stdout").read_text(encoding="utf-8", errors="replace")
        with contextlib.suppress(OSError):
            stderr = (out_dir / "stderr").read_text(encoding="utf-8", errors="replace")

        session_id = job.args.get("session_id") or ""
        duration_ms = int(((job.finished_at or time.time()) - job.started_at) * 1000)
        # We don't know the actual returncode. If the JSON parses cleanly
        # we treat the run as successful; otherwise as an error.
        result = self._finalize_payload(
            channel=job.channel,
            session_id=str(session_id),
            duration_ms=duration_ms,
            returncode=0 if stdout.strip() else 1,
            stdout=stdout,
            stderr=stderr,
        )
        job.status = "done" if result.ok else "error"
        job.result = result.to_dict()
        job.error = None if result.ok else result.error
        job.finished_at = time.time()
        await self._save_jobs()
        self._log_event(
            "dispatch_orphan_finalized",
            job_id=job.id,
            channel=job.channel,
            ok=result.ok,
        )

    async def ensure_watchers_running(self) -> None:
        """Spawn watchers for any persisted ``running`` jobs and bring up
        the scheduler loop if it isn't already running.

        Called from every async MCP tool entry. Safe and cheap to call
        repeatedly; only spawns one watcher per job and one scheduler
        task per Dispatcher.
        """
        for job in self._jobs.values():
            if job.status == "running" and job.task is None:
                self._spawn_watcher(job)
        if self._scheduler_task is None or self._scheduler_task.done():
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            self._scheduler_task = loop.create_task(
                self._scheduler_loop(), name="bridge-scheduler"
            )

    # ----- recurring dispatches (schedules) -----

    async def _scheduler_loop(self) -> None:
        """Wake periodically; for each active schedule, decide whether
        a tick is due and fire one if so.

        We never "catch up" missed ticks. If the bridge was down for an
        hour and a 5-minute schedule has 12 missed ticks, we fire one
        on the first iteration and then resume normal cadence. This
        avoids burst-firing after restarts and keeps the schedule
        intent ("check every N min") closer to the user's mental model.
        """
        while True:
            try:
                await self._tick_scheduler_once()
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001 — never let the loop die
                self._log_event(
                    "scheduler_loop_error",
                    error=f"{type(exc).__name__}: {exc}",
                )
            try:
                await asyncio.sleep(self._scheduler_poll_seconds)
            except asyncio.CancelledError:
                return

    async def _tick_scheduler_once(self) -> None:
        now = time.time()
        # Iterate a snapshot — schedules can be added/cancelled mid-loop.
        for schedule in list(self._schedules.values()):
            # Promote waiting → active when predecessor terminates.
            if schedule.status == "waiting":
                pred_id = schedule.after_schedule_id
                pred = self._schedules.get(pred_id) if pred_id else None
                if pred is None or pred.status in (
                    "completed",
                    "cancelled",
                    "error",
                ):
                    schedule.status = "active"
                    await self._save_schedules()
                    self._log_event(
                        "schedule_activated",
                        schedule_id=schedule.id,
                        channel=schedule.channel,
                        after_schedule_id=pred_id,
                        predecessor_status=(pred.status if pred else "missing"),
                    )
                else:
                    # Predecessor still running; nothing else to do this round.
                    continue

            if schedule.status != "active":
                continue

            # 1. Did the last tick emit the stop sentinel? If so,
            #    self-cancel before considering whether to fire again.
            if schedule.last_job_id:
                last_job = self._jobs.get(schedule.last_job_id)
                if last_job is not None and last_job.status == "done":
                    last_text = ""
                    if last_job.result is not None:
                        last_text = str(last_job.result.get("result") or "")
                    if STOP_SENTINEL in last_text:
                        schedule.status = "cancelled"
                        schedule.error = (
                            f"tick {schedule.tick_count} emitted "
                            f"{STOP_SENTINEL}"
                        )
                        await self._save_schedules()
                        self._log_event(
                            "schedule_self_cancelled",
                            schedule_id=schedule.id,
                            tick_count=schedule.tick_count,
                        )
                        await self._maybe_notify_schedule(
                            schedule,
                            "tick_with_sentinel",
                            last_tick_result=last_text,
                        )
                        await self._maybe_notify_schedule(
                            schedule,
                            "schedule_end",
                            last_tick_result=last_text,
                        )
                        continue

            # 2. Past the until?
            if schedule.until is not None and now >= schedule.until:
                schedule.status = "completed"
                await self._save_schedules()
                self._log_event(
                    "schedule_completed",
                    schedule_id=schedule.id,
                    tick_count=schedule.tick_count,
                    reason="until_reached",
                )
                last_text = self._last_tick_result_text(schedule)
                await self._maybe_notify_schedule(
                    schedule, "schedule_end", last_tick_result=last_text
                )
                continue

            # 3. Is a tick due?
            if schedule.last_tick_at is None:
                due = True
            else:
                due = (now - schedule.last_tick_at) >= schedule.interval_seconds
            if not due:
                continue

            # 4. If the prior tick is still running, skip this one.
            if schedule.last_job_id:
                last_job = self._jobs.get(schedule.last_job_id)
                if last_job is not None and last_job.status == "running":
                    continue

            # 5. Fire.
            try:
                job_id = await self.dispatch_async(
                    prompt=schedule.prompt,
                    channel=schedule.channel,
                    timeout_seconds=int(
                        schedule.args.get("timeout_seconds", 300)
                    ),
                    permission_mode=str(
                        schedule.args.get("permission_mode")
                        or "acceptEdits"
                    ),
                    cwd=schedule.args.get("cwd"),
                )
            except Exception as exc:  # noqa: BLE001
                schedule.status = "error"
                schedule.error = f"{type(exc).__name__}: {exc}"
                await self._save_schedules()
                self._log_event(
                    "schedule_tick_error",
                    schedule_id=schedule.id,
                    error=schedule.error,
                )
                await self._maybe_notify_schedule(schedule, "tick_error")
                await self._maybe_notify_schedule(schedule, "schedule_end")
                continue
            schedule.last_tick_at = now
            schedule.last_job_id = job_id
            schedule.tick_count += 1
            await self._save_schedules()
            self._log_event(
                "schedule_tick",
                schedule_id=schedule.id,
                channel=schedule.channel,
                job_id=job_id,
                tick_count=schedule.tick_count,
            )
            await self._maybe_notify_schedule(schedule, "tick")

    def _last_tick_result_text(self, schedule: Schedule) -> str | None:
        if not schedule.last_job_id:
            return None
        job = self._jobs.get(schedule.last_job_id)
        if job is None or job.result is None:
            return None
        return str(job.result.get("result") or "")

    async def create_schedule(
        self,
        prompt: str,
        channel: str,
        interval_seconds: float,
        until: str | None = None,
        until_seconds: float | None = None,
        timeout_seconds: int = 300,
        permission_mode: str = "acceptEdits",
        cwd: str | None = None,
        after_schedule_id: str | None = None,
        notify_url: str | None = None,
        notify_on: list[str] | None = None,
        notify_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Create a recurring dispatch.

        Returns either ``{"ok": True, "schedule_id": ..., "schedule": ...}``
        or ``{"ok": False, "error": ...}`` for validation failures.
        Does not raise — same fail-soft contract as ``dispatch``.

        ``after_schedule_id`` chains schedules: this one stays in
        ``waiting`` status until the named predecessor reaches a
        terminal state (completed, cancelled, or error). The chain is
        validated for existence and cycles.

        ``notify_url`` opts the schedule into webhook notifications;
        see ``_maybe_notify_schedule``.
        """
        if not prompt.strip():
            return {"ok": False, "error": "prompt is empty"}
        if not channel.strip():
            return {"ok": False, "error": "channel is empty"}
        if interval_seconds < self._min_schedule_interval_seconds:
            return {
                "ok": False,
                "error": (
                    f"interval_seconds must be at least "
                    f"{self._min_schedule_interval_seconds}s "
                    "(prevents runaway tick storms)"
                ),
            }
        if until is not None and until_seconds is not None:
            return {
                "ok": False,
                "error": "specify either 'until' or 'until_seconds', not both",
            }

        until_epoch: float | None = None
        if until is not None:
            until_epoch = self._parse_until(until)
            if until_epoch is None:
                return {
                    "ok": False,
                    "error": (
                        f"invalid 'until' (expected ISO 8601 like "
                        f"'2026-04-27T20:00:00Z'): {until!r}"
                    ),
                }
        elif until_seconds is not None:
            if until_seconds <= 0:
                return {"ok": False, "error": "until_seconds must be positive"}
            until_epoch = time.time() + float(until_seconds)

        # Validate chain.
        initial_status = "active"
        if after_schedule_id is not None:
            predecessor = self._schedules.get(after_schedule_id)
            if predecessor is None:
                return {
                    "ok": False,
                    "error": f"unknown after_schedule_id: {after_schedule_id}",
                }
            # Cycle detection — walk the chain and ensure we don't loop.
            seen: set[str] = set()
            cur: str | None = after_schedule_id
            while cur is not None:
                if cur in seen:
                    return {
                        "ok": False,
                        "error": (
                            f"after_schedule_id chain has a cycle "
                            f"involving {cur}"
                        ),
                    }
                seen.add(cur)
                step = self._schedules.get(cur)
                cur = step.after_schedule_id if step else None
            # If predecessor already terminal, start active immediately.
            if predecessor.status in ("completed", "cancelled", "error"):
                initial_status = "active"
            else:
                initial_status = "waiting"

        schedule = Schedule(
            id=str(uuid.uuid4()),
            prompt=prompt,
            channel=channel,
            interval_seconds=float(interval_seconds),
            until=until_epoch,
            args={
                "timeout_seconds": int(timeout_seconds),
                "permission_mode": permission_mode,
                "cwd": cwd,
            },
            created_at=time.time(),
            status=initial_status,
            after_schedule_id=after_schedule_id,
            notify_url=notify_url,
            notify_on=list(notify_on or []),
            notify_headers=dict(notify_headers or {}),
        )
        self._schedules[schedule.id] = schedule
        await self._save_schedules()
        self._log_event(
            "schedule_created",
            schedule_id=schedule.id,
            channel=channel,
            interval_seconds=interval_seconds,
            until=until_epoch,
            after_schedule_id=after_schedule_id,
            initial_status=initial_status,
        )
        return {"ok": True, "schedule_id": schedule.id, "schedule": schedule.public_view()}

    @staticmethod
    def _parse_until(s: str) -> float | None:
        """Parse an ISO 8601 string into an epoch timestamp; ``None`` on
        failure. Python 3.11's ``fromisoformat`` accepts the trailing ``Z``."""
        try:
            dt = _dt.datetime.fromisoformat(s)
        except ValueError:
            return None
        if dt.tzinfo is None:
            # Naive — treat as local time, same as datetime.timestamp() does.
            return dt.timestamp()
        return dt.timestamp()

    def list_schedules(self) -> list[dict[str, Any]]:
        return [s.public_view() for s in self._schedules.values()]

    def get_schedule(self, schedule_id: str) -> dict[str, Any]:
        sched = self._schedules.get(schedule_id)
        if sched is None:
            return {"ok": False, "error": f"unknown schedule_id: {schedule_id}"}
        return {"ok": True, "schedule": sched.public_view()}

    async def cancel_schedule(self, schedule_id: str) -> dict[str, Any]:
        sched = self._schedules.get(schedule_id)
        if sched is None:
            return {"ok": False, "error": f"unknown schedule_id: {schedule_id}"}
        if sched.status not in ("active", "waiting"):
            return {
                "ok": True,
                "cancelled": False,
                "reason": "already_inactive",
                "status": sched.status,
            }
        sched.status = "cancelled"
        await self._save_schedules()
        self._log_event(
            "schedule_cancelled",
            schedule_id=schedule_id,
            tick_count=sched.tick_count,
        )
        last_text = self._last_tick_result_text(sched)
        await self._maybe_notify_schedule(
            sched, "schedule_end", last_tick_result=last_text
        )
        return {"ok": True, "cancelled": True, "schedule": sched.public_view()}

    # ----- completion polling -----

    def list_completions(
        self, since: float = 0.0, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Jobs whose ``finished_at > since``, oldest finish first."""
        out: list[dict[str, Any]] = []
        for job in self._jobs.values():
            fin = job.finished_at
            if fin is None or fin <= since:
                continue
            entry = self.get_dispatch(job.id)
            if "raw" in entry:
                entry = {k: v for k, v in entry.items() if k != "raw"}
            entry["finished_at"] = fin
            out.append(entry)
        out.sort(key=lambda e: e.get("finished_at") or 0.0)
        return out[:limit] if limit > 0 else out

    async def wait_any_completion(
        self, since: float = 0.0, max_wait_seconds: float = 50.0
    ) -> list[dict[str, Any]]:
        """Block up to ``max_wait_seconds`` for at least one completion
        whose ``finished_at > since``. Polls at 1s — fast enough for
        Cowork's "anything new at the start of this turn?" pattern."""
        deadline = time.monotonic() + max_wait_seconds
        while True:
            comps = self.list_completions(since=since)
            if comps:
                return comps
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return []
            await asyncio.sleep(min(1.0, remaining))

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
        notify_url: str | None = None,
        notify_on: list[str] | None = None,
        notify_headers: dict[str, str] | None = None,
    ) -> str:
        """Kick off ``dispatch`` in the background; return a ``job_id``.

        Use this when the round trip might exceed the MCP transport's
        per-call ceiling (~60s). Channel locking still applies — concurrent
        async dispatches on the same channel queue up.

        Optional ``notify_url`` triggers a webhook POST when the job
        reaches a terminal state matching ``notify_on`` (default
        ``["done"]``; see ``_maybe_notify_job`` for the payload shape).

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
            notify_url=notify_url,
            notify_on=list(notify_on or []),
            notify_headers=dict(notify_headers or {}),
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

        Two cancellation paths matter here:

        * **User-requested** (``cancel_dispatch`` set ``cancel_requested``
          and SIGTERM'd the subprocess). Status becomes ``cancelled``;
          the subprocess is already on its way out.
        * **Runtime-induced** (FastMCP cancelling the task because the
          MCP transport disconnected, the asyncio loop tearing down,
          etc.). Status becomes ``abandoned``. Critically, we do *not*
          kill the subprocess — it's running in its own session, output
          is going to files. A subsequent bridge run (via the
          ``_recover_running_jobs`` watcher) will pick it up when it
          finishes and finalize the result.

        Without this distinction, a transport hiccup would silently
        mark every in-flight job as user-cancelled.
        """
        try:
            result = await self.dispatch(job=job, **dispatch_kwargs)
        except asyncio.CancelledError:
            if job.cancel_requested:
                job.status = "cancelled"
                job.error = "cancelled by caller"
                self._log_event(
                    "dispatch_cancelled", job_id=job.id, channel=job.channel
                )
            else:
                job.status = "abandoned"
                job.error = (
                    "asyncio task was cancelled by the runtime "
                    "(transport disconnect, loop shutdown). "
                    "Subprocess may still be running; it will be "
                    "finalized by the bridge's watcher."
                )
                self._log_event(
                    "dispatch_abandoned",
                    job_id=job.id,
                    channel=job.channel,
                    pid=job.pid,
                )
                # Spawn a watcher that will reap this orphan when it exits.
                self._spawn_watcher(job)
            job.finished_at = time.time()
            await self._save_jobs()
            await self._maybe_notify_job(job, job.status)
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
            await self._maybe_notify_job(job, "error")
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
        await self._maybe_notify_job(job, "done")
        return result

    def _live_status(self, job: Job) -> dict[str, Any]:
        """Read status from the in-memory ``asyncio.Task``.

        If the task is still in flight, return ``running``. If it's done
        (any way — completion, cancellation, exception), defer to the
        persisted ``Job`` fields. ``_tracked_dispatch`` always updates
        those fields before its task transitions to done, so they're
        the authoritative record of what happened.

        This matters most for distinguishing user-cancel from
        runtime-cancel: the asyncio.Task ends in the ``cancelled`` state
        either way, but ``Job.status`` carries the right answer
        (``cancelled`` vs ``abandoned``).
        """
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
        return self._persisted_status(job)

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

        Two-step cancel: set ``cancel_requested`` (so ``_tracked_dispatch``
        knows this came from the user), SIGTERM the subprocess by PID
        (so the work actually stops, even if the asyncio task was lost
        somehow), and cancel the task. Returns ``{"cancelled": true}``
        if anything was actionable; ``{"cancelled": false}`` for jobs
        that have already finished.
        """
        job = self._jobs.get(job_id)
        if job is None:
            return {"ok": False, "error": f"unknown job_id: {job_id}"}
        base = {"job_id": job.id, "channel": job.channel}
        task = job.task
        if task is not None and task.done():
            return {**base, "cancelled": False, "reason": "already_finished"}
        if task is None and job.status in (
            "done",
            "cancelled",
            "error",
            "orphaned",
            "abandoned",
        ):
            return {
                **base,
                "cancelled": False,
                "reason": "no_live_task",
                "status": job.status,
            }
        job.cancel_requested = True
        # Try to SIGTERM the subprocess if we have its PID. Always
        # safe — ``os.kill`` raises ProcessLookupError if the PID is
        # gone, which we suppress.
        if job.pid is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(job.pid), signal.SIGTERM)
        if task is not None and not task.done():
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
