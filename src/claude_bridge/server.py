"""MCP server entry point for claude-bridge.

Runs over stdio so the host (Cowork, Claude Code, or any MCP client) can
launch it via ``docker exec -i <container> claude-bridge``. The exposed
tool surface comes in two halves:

* **Synchronous** (best when the round trip fits inside the MCP
  transport's per-call ceiling, typically ~60s):

  - ``dispatch(prompt, channel, ...)``       — run a prompt and return the
    result, pinning a session per channel.

* **Asynchronous** (best for prompts that exceed the MCP transport
  ceiling — persona runs, multi-step refactors, anything that does real
  work in a busy project):

  - ``dispatch_async(prompt, channel, ...)`` — kick off in the background,
    return a ``job_id`` immediately.
  - ``get_dispatch(job_id)``                 — poll status, non-blocking.
  - ``wait_dispatch(job_id, max_wait_seconds)`` — block up to ~50s for a
    job, then return current state. Cowork polls this in a loop.
  - ``cancel_dispatch(job_id)``              — kill a running job.
  - ``list_jobs()``                           — summary of all tracked jobs.

* **Channel admin** (no claude invocation):

  - ``list_channels()``                       — channel→session_id map.
  - ``reset_channel(channel)``                — drop a channel's pinned
    session so the next dispatch starts fresh.

Configuration is environment-only (set in the container, not negotiated over
MCP):

* ``CLAUDE_BRIDGE_STATE`` — JSON state path.
  Default: ``$HOME/.claude-bridge/sessions.json``.
* ``CLAUDE_BRIDGE_CWD``   — working directory for ``claude -p``. Default: the
  current working directory of the bridge process (typically the project
  root that the host bind-mounts in).
* ``CLAUDE_BRIDGE_CLAUDE_BIN`` — name of the ``claude`` binary. Default:
  ``claude``. Override if it lives somewhere unusual.
* ``CLAUDE_BRIDGE_DEFAULT_PERMISSION_MODE`` — default permission mode if the
  caller does not supply one. Default: ``acceptEdits``.
* ``CLAUDE_BRIDGE_LOG`` — append-only JSONL event log. Default: disabled.
  One line per state transition (``dispatch_start``, ``dispatch_end``,
  ``dispatch_cancelled``, ``dispatch_error``, ``bridge_init_orphans``).
  Set to a path the bridge user can write (e.g. ``/home/vscode/.claude-bridge/bridge.log``).
* ``CLAUDE_BRIDGE_PERSIST_PROMPTS`` — ``1`` to include prompts in the
  on-disk job records. Off by default; container is the trust boundary
  but file dumps are easier to share than memory dumps.
* ``CLAUDE_BRIDGE_LOG_PROMPTS`` — ``1`` to include prompts in the JSONL
  log. Off by default for the same reason.
"""

from __future__ import annotations

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .dispatcher import Dispatcher

_STATE_PATH = Path(
    os.environ.get(
        "CLAUDE_BRIDGE_STATE",
        str(Path.home() / ".claude-bridge" / "sessions.json"),
    )
)
_DEFAULT_CWD = os.environ.get("CLAUDE_BRIDGE_CWD") or os.getcwd()
_CLAUDE_BIN = os.environ.get("CLAUDE_BRIDGE_CLAUDE_BIN", "claude")
_DEFAULT_PERMISSION_MODE = os.environ.get(
    "CLAUDE_BRIDGE_DEFAULT_PERMISSION_MODE", "acceptEdits"
)
_LOG_PATH_ENV = os.environ.get("CLAUDE_BRIDGE_LOG")
_LOG_PATH = Path(_LOG_PATH_ENV) if _LOG_PATH_ENV else None
_PERSIST_PROMPTS = os.environ.get("CLAUDE_BRIDGE_PERSIST_PROMPTS") == "1"
_LOG_PROMPTS = os.environ.get("CLAUDE_BRIDGE_LOG_PROMPTS") == "1"

mcp: FastMCP = FastMCP("claude-bridge")
dispatcher = Dispatcher(
    state_path=_STATE_PATH,
    claude_bin=_CLAUDE_BIN,
    default_cwd=_DEFAULT_CWD,
    log_path=_LOG_PATH,
    persist_prompts=_PERSIST_PROMPTS,
    log_prompts=_LOG_PROMPTS,
)


@mcp.tool()
async def dispatch(
    prompt: str,
    channel: str = "default",
    timeout_seconds: int = 300,
    permission_mode: str | None = None,
    cwd: str | None = None,
) -> dict:
    """Run a prompt against Claude Code inside this devcontainer.

    Each ``channel`` pins to its own Claude Code session. Concurrent
    dispatches across distinct channels are isolated; calls to the same
    channel are serialized and share conversation history. Pick a stable
    channel name per logical thread, e.g. ``"feature-auth"`` or
    ``"review-pr-123"``.

    Args:
        prompt: The natural-language task for Claude Code.
        channel: Logical conversation thread. Default: ``"default"``.
        timeout_seconds: Wall-clock seconds before the call is aborted.
            Default: 300.
        permission_mode: One of ``default``, ``acceptEdits``,
            ``bypassPermissions``, ``plan``. Defaults to the value of
            ``CLAUDE_BRIDGE_DEFAULT_PERMISSION_MODE`` (``acceptEdits``).
        cwd: Working directory for ``claude -p`` for this call only.
            Overrides the bridge-wide default (``CLAUDE_BRIDGE_CWD`` or
            the bridge's own cwd). Use this to point a single dispatch
            at a different repo without standing up a second bridge —
            e.g. anchor the bridge in a clean directory for fast cold
            starts and pass ``cwd="/workspace"`` only when you need to
            operate in a project with a heavy ``.claude/`` config.

    Returns:
        On success::

            {"ok": true, "channel": "...", "duration_ms": 1234,
             "result": "...", "session_id": "..."}

        On failure::

            {"ok": false, "channel": "...", "duration_ms": 1234,
             "error": "...", "exit_code": 1}
    """
    res = await dispatcher.dispatch(
        prompt=prompt,
        channel=channel,
        timeout_seconds=timeout_seconds,
        permission_mode=permission_mode or _DEFAULT_PERMISSION_MODE,
        cwd=cwd,
    )
    return res.to_dict()


@mcp.tool()
async def dispatch_async(
    prompt: str,
    channel: str = "default",
    timeout_seconds: int = 300,
    permission_mode: str | None = None,
    cwd: str | None = None,
) -> dict:
    """Kick off a dispatch in the background, return a ``job_id`` immediately.

    Use this when the prompt may take longer than the MCP transport's
    per-call ceiling (typically ~60s) — anything that does real work in a
    busy project workspace. The returned ``job_id`` is the handle for
    ``get_dispatch``, ``wait_dispatch``, and ``cancel_dispatch``.

    Same-channel calls still serialize (the channel lock is held by the
    background task), so concurrent ``dispatch_async`` on a single channel
    will queue. Distinct channels run in parallel.

    Args mirror ``dispatch``. Empty prompts are rejected synchronously
    with a structured error (no job is created).

    Returns::

        {"job_id": "...", "channel": "...", "ok": true}

    Or, on validation failure::

        {"ok": false, "error": "prompt is empty"}
    """
    await dispatcher.ensure_watchers_running()
    try:
        job_id = await dispatcher.dispatch_async(
            prompt=prompt,
            channel=channel,
            timeout_seconds=timeout_seconds,
            permission_mode=permission_mode or _DEFAULT_PERMISSION_MODE,
            cwd=cwd,
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "job_id": job_id, "channel": channel}


@mcp.tool()
def get_dispatch(job_id: str) -> dict:
    """Return the current state of a job. Non-blocking.

    ``status`` is one of ``running``, ``done``, ``cancelled``, ``error``.
    For ``done``, the returned dict carries the same fields as a
    synchronous ``dispatch`` result (``result``, ``session_id``, etc.).
    Unknown ``job_id`` returns ``{"ok": false, "error": ...}``.
    """
    return dispatcher.get_dispatch(job_id)


@mcp.tool()
async def wait_dispatch(job_id: str, max_wait_seconds: int = 50) -> dict:
    """Block up to ``max_wait_seconds`` for a job, then return state.

    Default 50s is intentionally below the typical MCP transport ceiling
    (~60s) — call this in a loop until ``status != "running"``. The
    underlying job is shielded from cancellation, so an aborted MCP call
    on this poller doesn't kill the work in flight.
    """
    await dispatcher.ensure_watchers_running()
    return await dispatcher.wait_dispatch(job_id, max_wait_seconds)


@mcp.tool()
def cancel_dispatch(job_id: str) -> dict:
    """Request cancellation of a running job.

    The task's ``CancelledError`` handler kills the underlying ``claude -p``
    subprocess before propagating, so we don't leave headless workers
    behind. Returns ``{"cancelled": true}`` if the request was sent;
    ``{"cancelled": false, "reason": "already_finished"}`` if the job had
    already returned.
    """
    return dispatcher.cancel_dispatch(job_id)


@mcp.tool()
def list_jobs() -> dict:
    """Summary of every tracked job — running and finished.

    Useful for diagnostics; not needed for normal dispatch flow.
    """
    return {"jobs": dispatcher.list_jobs()}


@mcp.tool()
def list_channels() -> dict:
    """List active channels and their pinned Claude Code session IDs."""
    return {"channels": dispatcher.list_channels()}


@mcp.tool()
def reset_channel(channel: str) -> dict:
    """Forget the pinned session for a channel.

    The next ``dispatch`` on this channel will start a fresh Claude Code
    session. Useful when a conversation has gone off the rails or when you
    want a clean slate without restarting the bridge.
    """
    existed = dispatcher.reset_channel(channel)
    return {"reset": existed, "channel": channel}


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
