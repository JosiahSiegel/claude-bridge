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

* **Atomic state writes.** The channel→session map is persisted to a JSON
  file written via a tempfile + ``replace`` so a crash during write never
  leaves a half-baked file.

* **Fail-soft.** Subprocess failures, timeouts, and JSON parse errors all
  return a structured error dict rather than raising. The MCP layer above
  surfaces them to Cowork as tool errors.
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
class Dispatcher:
    state_path: Path
    claude_bin: str = "claude"
    default_cwd: str | None = None
    _channel_locks: dict[str, asyncio.Lock] = field(default_factory=dict, init=False)
    _state: dict[str, Any] = field(default_factory=dict, init=False)

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
