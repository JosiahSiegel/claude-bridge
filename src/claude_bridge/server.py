"""MCP server entry point for claude-bridge.

Runs over stdio so the host (Cowork, Claude Code, or any MCP client) can
launch it via ``docker exec -i <container> claude-bridge``. Three tools are
exposed:

* ``dispatch(prompt, channel, ...)``  — run a prompt against Claude Code in
  this container and return the result, pinning a session per channel.
* ``list_channels()``                 — show channel→session_id map.
* ``reset_channel(channel)``          — drop a channel's pinned session.

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

mcp: FastMCP = FastMCP("claude-bridge")
dispatcher = Dispatcher(
    state_path=_STATE_PATH,
    claude_bin=_CLAUDE_BIN,
    default_cwd=_DEFAULT_CWD,
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
