"""MCP server entry point for claude-bridge.

The server runs over stdio so the host (Cowork, Claude Code, or any MCP
client) can launch it via ``docker exec -i <container> claude-bridge``.

If you are an agent reading this file: the cheapest way to learn the
full capability surface, recommended workflows, and gotchas is to call
the ``bridge_help`` tool once. It returns a structured map keyed by
tool group, plus the canonical patterns for short, long, and recurring
work.

Tool surface, by group:

* **Help / discovery**
    - ``bridge_help()`` — structured guide to every tool, when to use
      each, and the canonical workflows.

* **Synchronous dispatch** (best for prompts that finish under the MCP
  transport's per-call ceiling, ~60s in Cowork):
    - ``dispatch(prompt, channel, ...)``

* **Asynchronous dispatch** (best for anything longer — persona runs,
  multi-step refactors, work in a busy project):
    - ``dispatch_async(prompt, channel, ...)`` → returns a ``job_id``.
    - ``wait_dispatch(job_id, max_wait_seconds)`` → poll loop. Default
      50s is below the MCP ceiling so you can re-enter cleanly.
    - ``get_dispatch(job_id)`` → non-blocking status.
    - ``cancel_dispatch(job_id)``
    - ``list_jobs()``

* **Recurring dispatch** (a schedule that fires the same prompt on a
  channel at an interval, until a deadline or until the prompt itself
  emits ``[BRIDGE_STOP_SCHEDULE]``):
    - ``schedule_dispatch(prompt, channel, interval_seconds, ...)``
    - ``list_schedules()``, ``get_schedule(...)``, ``cancel_schedule(...)``

* **Completion polling** (replaces "I have to keep polling get_dispatch"):
    - ``list_completions(since)`` → finished jobs since a cursor.
    - ``wait_any_completion(since, max_wait_seconds)`` → long-poll for
      the *next* completion. Same MCP-ceiling-aware pattern as
      ``wait_dispatch``.

* **Channel admin** (no claude invocation):
    - ``list_channels()``, ``reset_channel(channel)``

Configuration is environment-only (set in the container, not negotiated
over MCP):

* ``CLAUDE_BRIDGE_STATE`` — JSON state path. Default
  ``$HOME/.claude-bridge/sessions.json``. Adjacent files
  (``jobs.json``, ``schedules.json``, ``job-output/``) live in the same
  directory.
* ``CLAUDE_BRIDGE_CWD`` — default working directory for ``claude -p``.
  Per-call ``cwd=`` overrides.
* ``CLAUDE_BRIDGE_CLAUDE_BIN`` — name/path of the ``claude`` binary.
  Default ``claude``.
* ``CLAUDE_BRIDGE_DEFAULT_PERMISSION_MODE`` — default if the caller omits
  ``permission_mode``. Default ``acceptEdits``.
* ``CLAUDE_BRIDGE_LOG`` — append-only JSONL event log path. Default
  disabled. Records every state transition.
* ``CLAUDE_BRIDGE_PERSIST_PROMPTS`` — ``1`` to include prompts in the
  on-disk job records. Off by default.
* ``CLAUDE_BRIDGE_LOG_PROMPTS`` — ``1`` to include prompts in the JSONL
  log. Off by default.
"""

from __future__ import annotations

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .dispatcher import STOP_SENTINEL, Dispatcher

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


# ---------- discovery / help ----------


@mcp.tool()
def bridge_help() -> dict:
    """Return a structured guide to every tool, recommended workflow, and
    common gotcha. Call this once when you start using the bridge, or
    whenever you're unsure which tool fits a situation.

    The shape:

    * ``tools``: name → ``{group, summary, when_to_use, when_not_to_use}``
    * ``workflows``: list of named multi-step patterns with example
      invocations.
    * ``concepts``: short definitions of channel, session pinning,
      permission_mode, cwd, the stop sentinel, and the durability model.
    * ``gotchas``: things that have actually bitten users.
    """
    return {
        "name": "claude-bridge",
        "version": "0.1.0",
        "summary": (
            "MCP server that dispatches Claude Code prompts into a "
            "devcontainer. Subprocesses are decoupled from the asyncio "
            "task and the bridge process so work survives transport "
            "timeouts, MCP cancellations, and bridge restarts."
        ),
        "stop_sentinel": STOP_SENTINEL,
        "tools": {
            "dispatch": {
                "group": "synchronous",
                "summary": "Run a prompt and wait for the full result.",
                "when_to_use": (
                    "Short prompts you expect to finish under the MCP "
                    "transport ceiling (~60s in Cowork). Quick checks, "
                    "smoke tests, simple questions."
                ),
                "when_not_to_use": (
                    "Anything that might take longer — use "
                    "dispatch_async + wait_dispatch instead. If a "
                    "dispatch call exceeds the ceiling the transport "
                    "may abort the call before you see the result."
                ),
            },
            "dispatch_async": {
                "group": "asynchronous",
                "summary": "Spawn a background dispatch; return job_id.",
                "when_to_use": (
                    "Anything > ~30s: persona runs, refactors, work in a "
                    "project workspace with heavy MCP servers. The "
                    "subprocess is detached and writes output to disk, "
                    "so it survives transport timeouts and bridge "
                    "restarts."
                ),
                "when_not_to_use": (
                    "Trivial prompts where waiting an extra round trip "
                    "for wait_dispatch isn't worth it."
                ),
            },
            "wait_dispatch": {
                "group": "asynchronous",
                "summary": (
                    "Long-poll a job for up to ~50s; safe to call in a "
                    "loop until status != 'running'."
                ),
                "when_to_use": (
                    "Right after dispatch_async, in a polling loop. The "
                    "50s default is below the MCP ceiling, so each call "
                    "comes back cleanly. The underlying job is shielded "
                    "— if your wait call is cancelled, the job keeps "
                    "running and you can re-enter."
                ),
                "when_not_to_use": (
                    "If you want a non-blocking peek, use get_dispatch."
                ),
            },
            "get_dispatch": {
                "group": "asynchronous",
                "summary": "Non-blocking status read.",
                "when_to_use": (
                    "Quick polling at intervals you control (e.g. every "
                    "few seconds in a custom loop) or for one-shot "
                    "status checks."
                ),
                "when_not_to_use": (
                    "If you want to block until a result lands; use "
                    "wait_dispatch."
                ),
            },
            "cancel_dispatch": {
                "group": "asynchronous",
                "summary": (
                    "User-cancel a job. SIGTERMs the subprocess and "
                    "marks the job 'cancelled' (distinct from "
                    "'abandoned' which is a runtime/transport cancel)."
                ),
                "when_to_use": "When you want to stop the work.",
                "when_not_to_use": (
                    "When the job has already finished (it'll return "
                    "{cancelled: false, reason: 'already_finished'})."
                ),
            },
            "list_jobs": {
                "group": "asynchronous",
                "summary": "Summary of all tracked jobs (running + finished).",
                "when_to_use": (
                    "Diagnostics, dashboards. ``raw`` is stripped to "
                    "keep the response small; use get_dispatch for the "
                    "full payload of a specific job."
                ),
                "when_not_to_use": (
                    "If you have a specific job_id, prefer get_dispatch."
                ),
            },
            "schedule_dispatch": {
                "group": "recurring",
                "summary": (
                    "Recurring dispatch on a channel at an interval. "
                    "Each tick is its own dispatch_async job — "
                    "individually short, collectively long-running."
                ),
                "when_to_use": (
                    "Watch-this-condition-over-time work that doesn't "
                    "fit in a single long dispatch: 'every 5 min for the "
                    "next 4 hours, check open PRs and merge anything "
                    "ready.' The bridge owns the loop; you walk away."
                ),
                "when_not_to_use": (
                    "One-shot work — use dispatch or dispatch_async. "
                    "The minimum interval is 10s; intervals below that "
                    "are rejected."
                ),
            },
            "list_schedules": {
                "group": "recurring",
                "summary": "All schedules: active, completed, cancelled.",
                "when_to_use": "Discover what schedules exist.",
                "when_not_to_use": "If you have a specific schedule_id, prefer get_schedule.",
            },
            "get_schedule": {
                "group": "recurring",
                "summary": "Detailed view of one schedule including last_job_id and tick_count.",
                "when_to_use": "After scheduling something, to inspect progress.",
                "when_not_to_use": "For the latest result — use get_dispatch on last_job_id.",
            },
            "cancel_schedule": {
                "group": "recurring",
                "summary": "Stop a schedule from firing further ticks.",
                "when_to_use": (
                    "When you want to end a schedule before its until "
                    "or before the prompt emits the stop sentinel. "
                    "In-flight ticks continue but no new ones fire."
                ),
                "when_not_to_use": (
                    "If the schedule already ended; returns "
                    "{cancelled: false, reason: 'already_inactive'}."
                ),
            },
            "list_completions": {
                "group": "completion_polling",
                "summary": (
                    "Jobs whose finished_at > since, oldest finish "
                    "first. Cursor is whatever finished_at you saw "
                    "most recently."
                ),
                "when_to_use": (
                    "Top of every turn / iteration: 'anything new since "
                    "I last checked?' Doesn't block."
                ),
                "when_not_to_use": "If you want to wait for new completions, use wait_any_completion.",
            },
            "wait_any_completion": {
                "group": "completion_polling",
                "summary": (
                    "Long-poll up to ~50s for any new completion since "
                    "a cursor. Returns immediately if any are already "
                    "available."
                ),
                "when_to_use": (
                    "When you want to wait until SOMETHING finishes "
                    "without polling individual job_ids — useful for "
                    "schedule-driven workflows where ticks finish "
                    "asynchronously."
                ),
                "when_not_to_use": "If you know the specific job_id, wait_dispatch is more direct.",
            },
            "list_channels": {
                "group": "channels",
                "summary": "channel → pinned session_id.",
                "when_to_use": "Diagnostics; checking what conversations the bridge is tracking.",
                "when_not_to_use": "Doesn't tell you whether a job is running on a channel — use list_jobs for that.",
            },
            "reset_channel": {
                "group": "channels",
                "summary": "Drop a channel's session pinning so the next dispatch starts fresh.",
                "when_to_use": (
                    "When a session has gone off the rails (claude is "
                    "stuck looping, or a project MCP server has wedged "
                    "inside the session and reset is the cleanest fix). "
                    "Also: when you want a fresh context for the "
                    "channel without changing channel names."
                ),
                "when_not_to_use": (
                    "When the goal is to cancel an in-flight dispatch "
                    "— that's cancel_dispatch. reset_channel doesn't "
                    "cancel running work."
                ),
            },
        },
        "workflows": [
            {
                "name": "Short prompt (under ~60s)",
                "steps": [
                    "result = dispatch(prompt='...', channel='...')",
                    "if result['ok']: use result['result']",
                ],
                "example": (
                    'dispatch(prompt="ls /workspace and tell me what '
                    'you see", channel="filesystem-probe")'
                ),
            },
            {
                "name": "Long prompt (anything that might exceed the MCP ceiling)",
                "steps": [
                    "j = dispatch_async(prompt='...', channel='...', timeout_seconds=900)",
                    "while True:",
                    "    state = wait_dispatch(j['job_id'], max_wait_seconds=50)",
                    "    if state['status'] != 'running': break",
                    "use state['result']",
                ],
                "example": (
                    'dispatch_async(prompt="audit the auth middleware '
                    'and write a report", channel="auth-audit", '
                    'cwd="/workspace", timeout_seconds=600)'
                ),
            },
            {
                "name": "Watch a condition until it resolves (recurring)",
                "steps": [
                    "s = schedule_dispatch(prompt='check ... if X end with " + STOP_SENTINEL + "', channel='watcher', interval_seconds=300, until_seconds=14400)",
                    "# Walk away. Each tick fires every 5 min for up to 4 hours.",
                    "# Optional: in another turn, list_completions(since=cursor) to see ticks.",
                ],
                "example": (
                    "schedule_dispatch(prompt='gh pr list --state open. "
                    "If all merged, end with " + STOP_SENTINEL + "'. "
                    "Otherwise summarize.', channel='pr-watcher', "
                    "interval_seconds=300, until_seconds=14400, "
                    "permission_mode='bypassPermissions')"
                ),
            },
            {
                "name": "Surface new completions at the top of a turn",
                "steps": [
                    "comps = wait_any_completion(since=last_seen_finished_at, max_wait_seconds=2)",
                    "for c in comps: surface c['result']",
                    "last_seen_finished_at = max(c['finished_at'] for c in comps) if comps else last_seen_finished_at",
                ],
                "example": "wait_any_completion(since=1714000000.0, max_wait_seconds=2)",
            },
        ],
        "concepts": {
            "channel": (
                "A stable string identifier for a conversation thread. "
                "Each channel pins to one Claude Code session_id. "
                "First dispatch on a channel starts a fresh session; "
                "subsequent dispatches on the same channel resume it. "
                "Distinct channels run in parallel; same-channel calls "
                "serialize."
            ),
            "session_pinning": (
                "channel → session_id mapping is persisted to disk "
                "(sessions.json). Survives bridge restarts. Reset with "
                "reset_channel."
            ),
            "permission_mode": (
                "Passed through to claude -p --permission-mode. Values: "
                "default, acceptEdits, plan, bypassPermissions. The "
                "bridge default is set via CLAUDE_BRIDGE_DEFAULT_PERMISSION_MODE."
            ),
            "cwd": (
                "Per-call working directory for claude -p. Overrides "
                "the bridge-wide default. Use it to retarget at a "
                "specific repo without standing up another bridge."
            ),
            "stop_sentinel": (
                f"The literal string {STOP_SENTINEL}. If a scheduled "
                "tick's result contains it anywhere, the schedule "
                "self-cancels after that tick. Useful for watch-until-X "
                "patterns where the prompt knows the termination "
                "condition but the caller doesn't."
            ),
            "job_status": (
                "running, done, cancelled (user via cancel_dispatch), "
                "abandoned (runtime cancel — subprocess kept running, "
                "watcher will reap), error, orphaned (subprocess and "
                "output both lost). Poll again on 'abandoned' — the "
                "watcher transitions it to 'done' or 'error' shortly."
            ),
        },
        "durability": {
            "persists_to_disk": [
                "channel → session pinning (sessions.json)",
                "every job's full state including PID and output paths (jobs.json)",
                "every schedule's full state (schedules.json)",
                "subprocess stdout/stderr (job-output/<job_id>/)",
                "optional event log (CLAUDE_BRIDGE_LOG)",
            ],
            "survives": [
                "MCP transport timeouts (subprocess detached from asyncio task)",
                "FastMCP cancelling tool calls (job marked 'abandoned', watcher reaps)",
                "bridge process crashes (subprocess survives, output files intact)",
                "Claude Desktop restarts",
            ],
            "does_not_survive": [
                "container death (all subprocesses die)",
                "deletion of the state directory",
            ],
        },
        "gotchas": [
            (
                "Cwd matters a lot. A project workspace with heavy "
                ".claude/settings.local.json (multiple project MCP "
                "servers, plugins, big CLAUDE.md) can stall every "
                "dispatch on cold start. Anchor the bridge in a clean "
                "directory via CLAUDE_BRIDGE_CWD and pass cwd= "
                "per-call when you need project context."
            ),
            (
                "Wait_dispatch returning status='running' at the timeout "
                "is normal. Re-call it with the same job_id."
            ),
            (
                "Status='abandoned' is not user-cancellation. Poll "
                "get_dispatch a few seconds later — the watcher will "
                "transition it to 'done' or 'error' from the on-disk "
                "output files."
            ),
            (
                "Same-channel concurrent dispatches serialize behind a "
                "lock. If you fire dispatch_async twice on the same "
                "channel quickly, the second waits for the first."
            ),
            (
                "permission_mode='bypassPermissions' is only safe when "
                "the container's network/filesystem isolation is "
                "doing the work. Don't bypass on a host-mounted bridge."
            ),
            (
                "Schedules don't catch up after a long bridge outage — "
                "they fire one tick on the first iteration and resume "
                "normal cadence. This avoids burst-firing 60 ticks "
                "after a 1-hour outage on a 1-min schedule."
            ),
        ],
    }


# ---------- synchronous dispatch ----------


@mcp.tool()
async def dispatch(
    prompt: str,
    channel: str = "default",
    timeout_seconds: int = 300,
    permission_mode: str | None = None,
    cwd: str | None = None,
) -> dict:
    """Run a prompt against Claude Code in this devcontainer; return the result.

    Use for short prompts (under ~60s round trip). For anything longer
    use ``dispatch_async`` so the MCP transport's per-call ceiling
    doesn't abort your tool call before claude finishes.

    Args:
        prompt: The natural-language task for Claude Code.
        channel: Logical conversation thread. Same channel = shared
            session (subsequent calls ``--resume`` it). Default
            ``"default"`` — pick a stable channel name per logical
            thread (e.g. ``"feature-auth"``).
        timeout_seconds: Wall-clock seconds before the call is aborted.
            Default 300.
        permission_mode: ``default`` | ``acceptEdits`` | ``plan`` |
            ``bypassPermissions``. Defaults to
            ``CLAUDE_BRIDGE_DEFAULT_PERMISSION_MODE``.
        cwd: Per-call working directory override. Use it to retarget at
            a specific project (e.g. ``/workspace``) while keeping the
            bridge anchored in a clean directory for fast cold starts.

    Returns:
        Success: ``{ok: true, channel, duration_ms, result, session_id, raw, stderr}``.
        Failure: ``{ok: false, channel, duration_ms, error, exit_code?}``.
        Failures never raise — the MCP layer always sees a result.
    """
    res = await dispatcher.dispatch(
        prompt=prompt,
        channel=channel,
        timeout_seconds=timeout_seconds,
        permission_mode=permission_mode or _DEFAULT_PERMISSION_MODE,
        cwd=cwd,
    )
    return res.to_dict()


# ---------- asynchronous dispatch ----------


@mcp.tool()
async def dispatch_async(
    prompt: str,
    channel: str = "default",
    timeout_seconds: int = 300,
    permission_mode: str | None = None,
    cwd: str | None = None,
) -> dict:
    """Spawn a dispatch in the background, return a ``job_id`` immediately.

    Use this for anything that may exceed the MCP transport's per-call
    ceiling (~60s in Cowork) — persona runs, refactors, anything in a
    busy project. The subprocess is spawned with its own session
    (``start_new_session=True``) and its stdout/stderr go to files on
    disk, so the work survives transport timeouts and bridge restarts.

    Then poll with ``wait_dispatch(job_id)`` in a loop until
    ``status != 'running'``.

    Args mirror ``dispatch``. Empty prompts return a structured error
    synchronously without creating a job.

    Returns:
        Success: ``{ok: true, job_id, channel}``.
        Validation failure: ``{ok: false, error}``.
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
    """Non-blocking read of a job's current state.

    ``status`` values:

    * ``running`` — work in flight; ``elapsed_ms`` included for progress.
    * ``done`` — full sync-style result keys.
    * ``cancelled`` — user called ``cancel_dispatch``.
    * ``abandoned`` — runtime cancel (transport timeout, FastMCP shutdown).
      Subprocess kept running; watcher will transition this to ``done``
      or ``error`` shortly. Poll again.
    * ``error`` — dispatcher-internal error or unparseable output.
    * ``orphaned`` — subprocess and output both lost on a restart.

    Unknown ``job_id`` returns ``{ok: false, error: ...}``. Works for
    both live jobs and ones loaded from disk after a bridge restart.
    """
    return dispatcher.get_dispatch(job_id)


@mcp.tool()
async def wait_dispatch(job_id: str, max_wait_seconds: int = 50) -> dict:
    """Long-poll a job for up to ``max_wait_seconds``.

    Default 50s is intentionally below the typical MCP transport
    ceiling (~60s) — call this in a loop and break when ``status !=
    'running'``. The underlying job is shielded from cancellation, so
    if your MCP call is aborted at the ceiling the work keeps running
    and you can re-enter with the same ``job_id``.
    """
    await dispatcher.ensure_watchers_running()
    return await dispatcher.wait_dispatch(job_id, max_wait_seconds)


@mcp.tool()
def cancel_dispatch(job_id: str) -> dict:
    """User-cancel a running job. SIGTERMs the subprocess by PID, then
    cancels the asyncio task. Status becomes ``cancelled`` (vs the
    ``abandoned`` state for runtime/transport-induced cancellations).

    Returns ``{cancelled: true}`` if the cancel was actionable, or
    ``{cancelled: false, reason}`` for jobs that have already finished
    or have no live task.
    """
    return dispatcher.cancel_dispatch(job_id)


@mcp.tool()
def list_jobs() -> dict:
    """All tracked jobs (running and finished). ``raw`` is stripped from
    done-state summaries to keep the response cheap; use
    ``get_dispatch(job_id)`` for the full payload.
    """
    return {"jobs": dispatcher.list_jobs()}


# ---------- recurring dispatch (schedules) ----------


@mcp.tool()
async def schedule_dispatch(
    prompt: str,
    channel: str,
    interval_seconds: float,
    until: str | None = None,
    until_seconds: float | None = None,
    timeout_seconds: int = 300,
    permission_mode: str | None = None,
    cwd: str | None = None,
) -> dict:
    """Recurring dispatch: fire ``prompt`` on ``channel`` every
    ``interval_seconds``, until a deadline or until the prompt emits
    the literal stop sentinel ``[BRIDGE_STOP_SCHEDULE]`` in its result.

    Each tick is an independent ``dispatch_async`` job — ticks are
    short individually, but the schedule can run for hours. The bridge
    owns the loop, persists schedules to disk, and resumes them after
    a restart (without burst-firing missed ticks).

    Args:
        prompt: The text fired at each tick.
        channel: Channel for every tick. Same-session continuity
            across ticks (each tick ``--resume``s the prior).
        interval_seconds: Seconds between ticks. Minimum 10.
        until: Absolute end time, ISO 8601 with TZ
            (e.g. ``"2026-04-27T20:00:00Z"``). Mutually exclusive with
            ``until_seconds``.
        until_seconds: Relative end time in seconds from now (e.g.
            ``14400`` = 4 hours).
        timeout_seconds: Per-tick timeout. Default 300.
        permission_mode: Same as ``dispatch``.
        cwd: Same as ``dispatch``.

    Returns:
        ``{ok: true, schedule_id, schedule}`` or ``{ok: false, error}``.

    Self-cancellation: if any tick's result text contains
    ``[BRIDGE_STOP_SCHEDULE]``, the schedule cancels after that tick.
    Use this in your prompt for "watch X until Y" patterns:

        "Check open PRs. If all merged, end your reply with
        [BRIDGE_STOP_SCHEDULE]. Otherwise summarize."
    """
    await dispatcher.ensure_watchers_running()
    return await dispatcher.create_schedule(
        prompt=prompt,
        channel=channel,
        interval_seconds=interval_seconds,
        until=until,
        until_seconds=until_seconds,
        timeout_seconds=timeout_seconds,
        permission_mode=permission_mode or _DEFAULT_PERMISSION_MODE,
        cwd=cwd,
    )


@mcp.tool()
def list_schedules() -> dict:
    """Every schedule the bridge knows about — active, completed, cancelled, error."""
    return {"schedules": dispatcher.list_schedules()}


@mcp.tool()
def get_schedule(schedule_id: str) -> dict:
    """Detail view of one schedule. Includes ``last_job_id`` so you can
    follow up with ``get_dispatch`` or ``list_completions``."""
    return dispatcher.get_schedule(schedule_id)


@mcp.tool()
async def cancel_schedule(schedule_id: str) -> dict:
    """Stop a schedule from firing further ticks. In-flight ticks are
    not cancelled — use ``cancel_dispatch(last_job_id)`` for that."""
    return await dispatcher.cancel_schedule(schedule_id)


# ---------- completion polling ----------


@mcp.tool()
def list_completions(since: float = 0.0, limit: int = 50) -> dict:
    """Jobs whose ``finished_at > since``, oldest first.

    Use ``since=0`` for "everything that ever finished". For ongoing
    polling, track the largest ``finished_at`` you've seen and pass it
    as the next ``since``. Cheap and non-blocking — safe to call at the
    start of every turn / iteration to surface "anything new?".

    Returns ``{completions: [<get_dispatch shape>, ...]}`` with
    ``finished_at`` present on each entry. ``raw`` is omitted; fetch
    full payloads via ``get_dispatch(job_id)``.
    """
    return {"completions": dispatcher.list_completions(since=since, limit=limit)}


@mcp.tool()
async def wait_any_completion(
    since: float = 0.0, max_wait_seconds: int = 50
) -> dict:
    """Long-poll up to ``max_wait_seconds`` for any new completion since
    the cursor. Returns immediately if any are already available;
    otherwise waits.

    Default 50s is below the MCP transport ceiling so you can re-enter
    in a loop. Useful when watching schedule ticks land without
    polling each tick's ``job_id`` separately.
    """
    await dispatcher.ensure_watchers_running()
    comps = await dispatcher.wait_any_completion(
        since=since, max_wait_seconds=max_wait_seconds
    )
    return {"completions": comps}


# ---------- channel admin ----------


@mcp.tool()
def list_channels() -> dict:
    """channel → pinned session_id. Doesn't tell you whether work is
    in flight on a channel — use ``list_jobs`` for that."""
    return {"channels": dispatcher.list_channels()}


@mcp.tool()
def reset_channel(channel: str) -> dict:
    """Drop a channel's pinned session so the next dispatch starts a
    fresh Claude Code session. Useful when a project MCP server has
    wedged inside the channel's session and you want a clean reconnect.

    Does not cancel running work — use ``cancel_dispatch`` for that.
    """
    existed = dispatcher.reset_channel(channel)
    return {"reset": existed, "channel": channel}


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
