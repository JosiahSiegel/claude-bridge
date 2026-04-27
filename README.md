# claude-bridge

An MCP server that lets host-side Claude Cowork dispatch work into
**Claude Code running inside your devcontainer**, over stdio. Cowork
launches the bridge with `docker exec -i`; the bridge shells to
`claude -p` and returns a structured result.

## Quickstart

Three things have to be true: the bridge installed in the container,
its absolute path in the Claude Desktop config on the host, and Claude
Desktop fully restarted.

**1. Install in the container.**

```bash
pip install claude-bridge
which claude-bridge      # copy this path — you'll paste it in step 2
```

**2. Register with Claude Desktop on the host.**

Open via `Settings → Developer → Edit Config`, or edit the file directly:

* macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
* Windows: `%APPDATA%\Claude\claude_desktop_config.json`
* Linux: `~/.config/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "devcontainer-claude": {
      "command": "docker",
      "args": [
        "exec", "-i",
        "-e", "CLAUDE_BRIDGE_CWD=/path/to/clean/dir",
        "<container-name-from-docker-ps>",
        "/absolute/path/from/which/claude-bridge"
      ]
    }
  }
}
```

The `CLAUDE_BRIDGE_CWD` line is important — see
[Recommended pattern](#recommended-pattern) below for why.

**3. Fully quit Claude Desktop** (`⌘Q` / system-tray exit, *not* close
window) and relaunch. In a Cowork session, ask Claude to call
`list_channels`. `{"channels": {}}` means it works.

**4. Dispatch.**

For quick prompts (under ~60s round trip):

```
dispatch(prompt="say ok", channel="smoke")
```

For real work (anything that might exceed the MCP transport's per-call
ceiling — persona runs, refactors, work in a busy project):

```
job = dispatch_async(
  prompt="audit the auth middleware",
  channel="auth-audit",
  cwd="/workspace",          # the project you want claude to work in
  timeout_seconds=900
)
# Then poll wait_dispatch(job["job_id"], max_wait_seconds=50) in a loop.
```

For watch-this-condition-over-hours work, use `schedule_dispatch` —
each tick is its own short job, the bridge owns the cron loop, and
the prompt can self-cancel by emitting `[BRIDGE_STOP_SCHEDULE]`.

```
schedule_dispatch(
  prompt="gh pr list --state open. If all merged, end with [BRIDGE_STOP_SCHEDULE]. Otherwise summarize.",
  channel="pr-watcher",
  interval_seconds=300,
  until_seconds=14400,        # stop trying after 4 hours
  permission_mode="bypassPermissions"
)
```

See [Tools → Polling pattern](#polling-pattern-the-canonical-long-running-flow)
for the long-job loop and [Recurring](#recurring-long-running-watch-patterns)
for schedules.

**For agents using the bridge programmatically**: call `bridge_help()`
first. It returns a structured guide to every tool, the four canonical
workflows, and the gotchas. Designed to be the single discoverability
entry point so agents don't have to skim the README.

## Recommended pattern

The bridge runs `claude -p` in its own cwd by default. If that cwd is a
busy project workspace (with `.claude/settings.local.json` enabling
several MCP servers and plugins, plus a large `CLAUDE.md`), every
dispatch pays a 30s+ cold-start cost — sometimes long enough to time
out.

The fix is to anchor the bridge in a clean directory and retarget
individual dispatches at the project they actually want:

* Set `CLAUDE_BRIDGE_CWD` in the MCP config to a clean dir (e.g. the
  bridge's own checkout, or any directory without `.claude/`).
* Pass `cwd="/workspace"` (or whatever) per dispatch when you need
  project context.
* Bump `timeout_seconds` for those calls — first invocation in a project
  has to load all its MCP servers.

`list_channels` and a bare `dispatch(prompt="say ok")` then stay snappy;
heavy project work gets routed to the right cwd on demand.

## Running unattended (max permissions)

For autonomous operation — letting Cowork drive the bridge without
permission prompts blocking dispatches — set the bridge's *default*
permission mode to `bypassPermissions` in the MCP config:

```json
{
  "mcpServers": {
    "devcontainer-claude": {
      "command": "docker",
      "args": [
        "exec", "-i",
        "-e", "CLAUDE_BRIDGE_CWD=/home/vscode/claude-bridge",
        "-e", "CLAUDE_BRIDGE_DEFAULT_PERMISSION_MODE=bypassPermissions",
        "<container>",
        "/absolute/path/to/claude-bridge"
      ]
    }
  }
}
```

Now every `dispatch(...)` without an explicit `permission_mode` runs as
`bypassPermissions` — no Bash gates, no workspace sandbox. Cowork
doesn't need to know the policy; it's enforced at the bridge. Individual
dispatches can still opt into stricter behavior by passing
`permission_mode="acceptEdits"` (or `"plan"`) per call.

**Only do this when the container is the trust boundary** — the
container's network and filesystem isolation is what's keeping bypassed
permissions safe. Treat the MCP config as privileged: anyone who can
edit it can run arbitrary commands in your container as the bridge
user. See the [official devcontainer guide][devcontainer] for the
recommended firewall config.

[devcontainer]: https://code.claude.com/docs/en/devcontainer

## Tools

> **For agents reading this**: call `bridge_help()` once at the start
> of a session. It returns a structured map of every tool, when to use
> each, the four canonical workflows, and the gotchas that have
> actually bitten users. Faster than skimming this section.

The tool surface comes in six groups:

* **Discovery**: `bridge_help`.
* **Synchronous**: `dispatch` — short prompts under the MCP ceiling.
* **Asynchronous**: `dispatch_async` + `wait_dispatch` /
  `get_dispatch` / `cancel_dispatch` / `list_jobs` — anything longer.
  Optional webhook on terminal state.
* **Recurring**: `schedule_dispatch` + `list_schedules` /
  `get_schedule` / `cancel_schedule` — fire a prompt every N seconds
  on a channel until a deadline or the prompt emits the stop sentinel.
  Supports chaining (`after_schedule_id`) and webhooks (`notify_url`).
* **Event log**: `list_events` — cursor-paged stream of every notable
  state transition. The "what happened while I was offline?" answer.
* **Completion polling**: `list_completions`, `wait_any_completion`
  — answer "anything new since I last looked?" for *finished jobs*.
  Use `list_events` instead when you need schedule context.
* **Channel admin**: `list_channels`, `reset_channel`.

### Synchronous

#### `dispatch(prompt, channel="default", timeout_seconds=300, permission_mode=None, cwd=None)`

Run a prompt against `claude -p` and return the full result. Channels
pin to one Claude Code session each — first call starts fresh,
subsequent calls on the same channel `--resume` it. Distinct channels
run in parallel; same-channel calls serialize.

* `permission_mode`: `default`, `acceptEdits`, `plan`, or
  `bypassPermissions`. Defaults to `acceptEdits` (override via
  `CLAUDE_BRIDGE_DEFAULT_PERMISSION_MODE`).
* `cwd`: per-call override of the working directory. Use it to retarget
  a single dispatch at a different repo without standing up a second
  bridge.

Returns `{ok, channel, duration_ms, result, session_id, raw}` on
success, or `{ok: false, channel, duration_ms, error, exit_code}` on
failure. Both shapes include `stderr` when claude wrote to it (e.g.
project-MCP-server warnings). **Failures never raise** — the MCP layer
always sees a result.

### Asynchronous (long-running)

#### `dispatch_async(prompt, channel="default", timeout_seconds=300, permission_mode=None, cwd=None, notify_url=None, notify_on=None, notify_headers=None)`

Kick off a dispatch in the background; return a `job_id` immediately.
Channel locking still applies — concurrent `dispatch_async` on the same
channel queue up. Empty prompts surface as `{"ok": false, "error": ...}`
synchronously (no orphan job).

Optional webhook on terminal state: pass `notify_url` to have the
bridge POST a JSON payload when the job ends. `notify_on` selects
which terminal states notify (values: `done`, `error`, `cancelled`,
`abandoned`; default `["done"]`). `notify_headers` adds auth.
Delivery is fire-and-log; failures are recorded as `webhook_failed`
events. Payload shape: `{event, job_id, channel, status, started_at,
finished_at, ok, result_preview, error}` (`result_preview` is
truncated to 4KB).

Returns `{ok: true, job_id, channel}`.

#### `get_dispatch(job_id)`

Non-blocking status read. `status` is one of:

* `running` — work in flight; also includes `elapsed_ms` for progress.
* `done` — full sync-style result keys (`ok`, `result`, `session_id`,
  `duration_ms`, `raw`, `stderr`, …) plus `job_id` and `started_at`.
* `cancelled` — `cancel_dispatch` was called by the user; subprocess
  was SIGTERMed.
* `abandoned` — the asyncio task running the dispatch was cancelled by
  the runtime (transport timeout, FastMCP shutdown, loop teardown).
  The subprocess kept running; a watcher will finalize it when it
  exits, transitioning the status to `done` (or `error`). Poll again.
* `error` — programmer error in the dispatcher itself, or output that
  couldn't be parsed; should be rare.
* `orphaned` — bridge restarted but the subprocess and output files
  are gone; result was lost. The channel pinning is auto-reset so the
  next dispatch starts fresh.

Unknown `job_id` returns `{ok: false, error: ...}`. Works for both live
jobs and ones loaded from disk after a restart.

#### `wait_dispatch(job_id, max_wait_seconds=50)`

Block up to `max_wait_seconds` for a job, then return whatever
`get_dispatch` would return. **Default 50s is intentionally below the
typical MCP transport ceiling** — Cowork polls this in a loop until
`status != "running"`. The underlying job is shielded from cancellation,
so an aborted poll doesn't kill the work in flight.

#### `cancel_dispatch(job_id)`

Request cancellation. The task's `CancelledError` handler kills the
underlying `claude -p` subprocess before propagating, so we don't leave
orphan workers. Idempotent: `{cancelled: true}` if cancellation was
requested, `{cancelled: false, reason: "already_finished"}` otherwise.

#### `list_jobs()`

Diagnostics only — returns one summary dict per tracked job (running
and recently finished). Job retention is bounded by `max_completed_jobs`
(default 1000), so this is safe to call on a long-lived bridge.

### Recurring (long-running watch patterns)

#### `schedule_dispatch(prompt, channel, interval_seconds, until=None, until_seconds=None, after_schedule_id=None, notify_url=None, notify_on=None, notify_headers=None, ...)`

Fires `prompt` on `channel` every `interval_seconds`, until a deadline
or until the prompt emits the literal stop sentinel
`[BRIDGE_STOP_SCHEDULE]` in its result. Each tick is its own
`dispatch_async` job — short individually, collectively long-running.
The bridge owns the loop, persists schedules to disk, and resumes them
after a restart **without burst-firing** missed ticks (only one tick
fires on the first iteration after a long gap).

* `interval_seconds` minimum is 10s.
* `until` is ISO 8601 (`"2026-04-27T20:00:00Z"`); `until_seconds` is
  relative seconds-from-now. Mutually exclusive.
* If a tick is still running when the next interval fires, the bridge
  skips that tick (no stacking). Schedules use the same channel for
  every tick, so ticks share session continuity.
* Self-cancellation: have the prompt end with `[BRIDGE_STOP_SCHEDULE]`
  when the watched condition resolves. Example:

  ```
  prompt = "gh pr list --state open. If all merged, end with [BRIDGE_STOP_SCHEDULE]. Otherwise summarize."
  schedule_dispatch(prompt, channel="pr-watcher", interval_seconds=300, until_seconds=14400)
  ```

* **Pipelines** — pass `after_schedule_id` to chain schedules. The new
  schedule starts in `waiting` and transitions to `active` when the
  predecessor reaches a terminal state (completed, cancelled, or
  error). Cycles are detected and rejected at creation:

  ```
  a = schedule_dispatch(prompt="wave A merge…",  channel="wave-a", interval_seconds=300, until_seconds=14400)
  b = schedule_dispatch(prompt="post-merge hygiene", channel="hygiene",
                         interval_seconds=600, until_seconds=3600,
                         after_schedule_id=a["schedule_id"])
  ```

* **Webhooks** — pass `notify_url` to get a JSON POST when the
  schedule reaches notable transitions. `notify_on` selects events:
  `tick`, `tick_with_sentinel`, `tick_error`, `schedule_end` (default
  `["schedule_end"]`). `notify_headers` adds auth. Delivery is
  fire-and-log; failures are logged as `webhook_failed` events.
  Payload includes `event`, `schedule_id`, `channel`, `tick_count`,
  `status`, `last_tick_result` (truncated to 4KB), `last_job_id`.

#### `list_schedules() / get_schedule(id) / cancel_schedule(id)`

Inspect or stop schedules. `cancel_schedule` fires the `schedule_end`
webhook (if configured) and does not cancel the in-flight tick — use
`cancel_dispatch(last_job_id)` for that.

### Event log (turn-level "what happened while I was offline?")

#### `list_events(since=0, limit=100, types=None, notable_only=False)`

Bridge-wide structured event stream. Records every state transition:
dispatch lifecycle, schedule lifecycle, webhook outcomes, recovery
actions. The buffer is bounded (default 1000) and persisted to
`events.json` so it survives restarts.

Two read modes:

* **Debug mode** (`notable_only=False`, default): every event,
  including chatter like `dispatch_start` and `schedule_tick`. Right
  for forensic post-mortem.
* **Surfacing mode** (`notable_only=True`): only state transitions
  worth surfacing to a human. Drops `dispatch_start`, `schedule_tick`,
  `schedule_created`, `webhook_sent`, `bridge_init_subprocess_alive`.
  Keeps every terminal transition and every failure. **This is what
  an orchestrator wants for "what should I tell the user about?"**

Cursor pattern: pass `since=0` for everything, then track the largest
`ts` you've seen. `types` is an explicit allow-list and composes with
`notable_only` (intersection). Returns oldest-first.

```
events = list_events(since=last_seen_ts, notable_only=True)
for e in events: surface(e)
last_seen_ts = max(e["ts"] for e in events) if events else last_seen_ts
```

Call `bridge_help()` and read `notable_event_types` to see the curated
set programmatically.

> **Heads-up**: events are only recorded from the moment the event-log
> feature is running. Anything that fired on a prior bridge process
> (or before this feature shipped) is gone. Going forward, every state
> transition is captured.

### Completion polling (turn-level "anything new?")

#### `list_completions(since=0, limit=50)`

Jobs whose `finished_at > since`, oldest first. Use `since=0` for
"everything that ever finished". For ongoing polling, track the
largest `finished_at` you've seen and pass it as the next `since`.
Cheap, non-blocking — safe at the top of every turn.

#### `wait_any_completion(since=0, max_wait_seconds=50)`

Long-poll up to `max_wait_seconds` for any new completion since the
cursor. Same MCP-ceiling-aware default as `wait_dispatch`. Returns
immediately if any are already available.

### Channel admin

#### `list_channels()`

`{"channels": {channel: session_id, ...}}`. Doesn't invoke `claude`.
Always cheap — Cowork can call this safely while a long dispatch is
running.

#### `reset_channel(channel)`

Drops a channel's pinned session so the next dispatch starts fresh.
`{"reset": true|false, "channel": ...}`. Useful when a project MCP
server (playwright, neon, etc.) has wedged inside the channel's claude
session and you want a clean reconnect.

### Polling pattern (the canonical long-running flow)

```
job = dispatch_async(prompt="...", channel="...", cwd="/workspace",
                     permission_mode="bypassPermissions",
                     timeout_seconds=900)

while True:
    res = wait_dispatch(job["job_id"], max_wait_seconds=50)
    if res["status"] != "running":
        break
    # optional: surface elapsed_ms, log, or just keep polling
```

`wait_dispatch` returning at the 50s mark with `status="running"` is the
common case for real work; the loop just goes around again. When the
job finishes, `wait_dispatch` returns immediately with the full result.

## Durability guarantees (long workflows)

The bridge is designed so Cowork can trust it across hours-long
workflows that span Claude Desktop restarts, container/bridge process
restarts, MCP transport hiccups, and Cowork's own per-call timeouts.
The contract:

### What persists

* **Channel→session pinning** in `sessions.json`, atomic temp+rename
  writes.
* **Every job's state** in `jobs.json` (same directory) on every
  transition: spawn (with PID and output-dir), completion, cancellation,
  abandonment. Concurrent writes are serialized through an
  `asyncio.Lock` so parallel dispatches can't lose each other's updates.
* **Subprocess output** in `<state>.parent/job-output/<job_id>/{stdout,stderr}`,
  written by `claude -p` directly (not via pipes the bridge has to
  drain). Output survives bridge crashes and asyncio task cancellations.

### Subprocess lifetime is decoupled from the bridge

For `dispatch_async`, the subprocess is spawned with
`start_new_session=True` (its own session, immune to SIGHUP) and
stdin redirected to `/dev/null`. Three follow-on guarantees:

* **MCP transport timeouts can't kill the work.** If FastMCP cancels
  the asyncio task running a dispatch (because Cowork's MCP request
  hit a transport timeout, or the connection blipped), the subprocess
  keeps running and writing output. The job is marked `abandoned`
  rather than `cancelled`, and a watcher coroutine reaps it when it
  finishes — finalizing the result from the on-disk output files.
* **Bridge crashes don't kill the work.** If the bridge process dies
  (OOM, supervisor restart, manual kill), the subprocess is parented
  to PID 1 and keeps running. On bridge restart:
  * If the PID is still alive, the job stays `running` and a watcher
    is re-spawned.
  * If the PID has exited and the output files are intact, the job is
    finalized — `get_dispatch(job_id)` returns the recovered result.
  * If neither, the job becomes `orphaned` and its channel is unpinned
    so a new dispatch can't race a stray subprocess on the same session.
* **`cancel_dispatch` is the only way to actually stop the work.** It
  sets a `cancel_requested` flag, SIGTERMs the subprocess by PID
  (then SIGKILLs if it's still alive on the watcher's next tick), and
  cancels the asyncio task. Status becomes `cancelled`. Anything else
  that ends a task (transport timeout, runtime cancellation, asyncio
  loop teardown) becomes `abandoned`, not `cancelled`.

### `wait_dispatch` is shielded

If Cowork's MCP call to `wait_dispatch` is cancelled by the transport
(e.g. exceeded the per-call ceiling), the underlying job survives —
the inner task is wrapped in `asyncio.shield`. Cowork retries with the
same `job_id` and gets the next slice of state.

### Failures never raise out of the MCP layer

Subprocess errors, timeouts, missing binary, malformed JSON, runtime
cancellation — all become structured `ok: false` results. The MCP
transport always sees a clean tool response.

### What this does **not** cover

* **Container death.** If the container itself dies, all subprocesses
  die with it. Nothing for the bridge to recover.
* **Output-file corruption** (e.g. disk full). If `claude -p`'s stdout
  is truncated, the recovery path treats it as a parse failure and
  marks the job as `error`.
* **Subprocess exit code on recovery.** When the bridge wasn't the
  parent at exit, we can't read the return code. Recovery treats
  well-formed JSON as success and unparseable output as failure — the
  exit code is informative but not load-bearing.

## Configuration (env vars in the container)

| Variable | Default | Purpose |
|---|---|---|
| `CLAUDE_BRIDGE_STATE` | `~/.claude-bridge/sessions.json` | Channel→session map (atomic writes). The same directory holds `jobs.json`, `schedules.json`, `job-output/<job_id>/{stdout,stderr}` and (if enabled) the JSONL log |
| `CLAUDE_BRIDGE_CWD` | bridge process cwd | **Default** working dir for `claude -p`. Per-call `cwd=` overrides |
| `CLAUDE_BRIDGE_CLAUDE_BIN` | `claude` | Override `claude` binary location |
| `CLAUDE_BRIDGE_DEFAULT_PERMISSION_MODE` | `acceptEdits` | Default if caller omits `permission_mode` |
| `CLAUDE_BRIDGE_LOG` | unset | If set to a path, writes one JSONL line per state transition (`dispatch_start`, `dispatch_end`, `dispatch_cancelled`, `dispatch_error`, `bridge_init_orphans`). Helps when something looks wrong at the bridge layer |
| `CLAUDE_BRIDGE_PERSIST_PROMPTS` | unset | Set to `1` to include the prompt text in `jobs.json`. Off by default; opt in for post-mortem debugging |
| `CLAUDE_BRIDGE_LOG_PROMPTS` | unset | Set to `1` to include prompts in the JSONL log too. Off by default |

Set these via your MCP config (`docker exec -e KEY=value ...`) or your
devcontainer's `containerEnv`. They are not negotiated over MCP.

## Authentication

The bridge has no auth opinion — it just shells to `claude -p` and
inherits the container's environment. Anything that makes
`claude -p "hi" --output-format json` succeed in your container shell
will work here, including:

* `ANTHROPIC_API_KEY`
* `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`)
* On-disk `~/.claude/.credentials.json` (from `claude /login`)

Test (validated by
[`test_auth_env_passes_through_to_subprocess`](tests/test_dispatcher.py)):
the bridge does **not** pass `env=` to the subprocess, so the
container's env reaches `claude` unchanged.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `OCI runtime exec failed: ... "claude-bridge": executable file not found in $PATH` | Bridge installed in a venv or `~/.local/bin` not on `docker exec`'s default `PATH` | Use the absolute path from `which claude-bridge` in the MCP config |
| `dispatch` hangs or times out, but `list_channels` returns instantly | The bridge's cwd has a heavy `.claude/`. `claude -p` is loading project MCP servers/plugins and stalling | Anchor the bridge in a clean dir with `CLAUDE_BRIDGE_CWD`, pass `cwd=...` and bump `timeout_seconds` per call. See [Recommended pattern](#recommended-pattern) |
| Cowork reports "lost the response handle" or hits a ~60s MCP ceiling on `dispatch` | The MCP transport caps individual tool calls; long claude runs exceed it | Use `dispatch_async` + `wait_dispatch`, not `dispatch`. Each `wait_dispatch` returns within 50s by design, and the underlying job survives across calls. See [Tools → Polling pattern](#polling-pattern-the-canonical-long-running-flow) |
| A project MCP server (playwright, neon, cloudflare, …) disconnects mid-session and the channel keeps failing | The wedged MCP server is bound to the pinned `claude -p` session for that channel | `reset_channel("<name>")`, then dispatch again — the next `claude -p` reconnects all its project MCP servers from scratch |
| Cowork doesn't see the server after editing the config | Window-close ≠ quit | Fully quit Claude Desktop and relaunch |
| Edits to the config produce no servers and no error | JSON syntax error | `python -m json.tool < claude_desktop_config.json` to validate. Claude Desktop silently ignores malformed files |
| `claude_desktop_config.json` `Settings → Connectors` UI doesn't list the bridge | That UI is for *remote* HTTP MCP servers only. Local stdio servers go in the JSON file | (Working as intended — you're not missing anything) |
| `D:/Program Files/Git/...` in the path when running `docker exec` from Windows Git Bash | MSYS rewrote your `/...` arg | `MSYS_NO_PATHCONV=1 docker exec ...` or double the leading slash (`//path`). Doesn't affect Claude Desktop's invocation, only your interactive testing |

## Verifying the bridge directly (optional)

For interactive smoke tests, the only useful CLI check is an MCP
`initialize` round-trip — there's no `--help`:

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}' \
  | docker exec -i <container> /absolute/path/to/claude-bridge
# Windows Git Bash: prefix the path with //
```

Expect a one-line JSON response with
`"serverInfo":{"name":"claude-bridge",...}`. Don't bother running
`docker exec ... claude -p "hi"` as a sanity check — that's a different
code path than the bridge uses, and it has Windows/Git Bash quirks of
its own.

## How it works

```
┌──────────────────────┐       ┌──────────────────────────────┐
│ HOST                 │ stdio │ DEVCONTAINER                 │
│ Claude Cowork ───────┼──────▶│ claude-bridge ─▶ claude -p   │
│ (MCP client)         │       │ (MCP server)    --resume sid │
└──────────────────────┘       └──────────────────────────────┘
   docker exec -i …
```

* **Transport**: MCP stdio over `docker exec -i`. No network sockets,
  no port forwarding, no shared bind mounts.
* **Sessions**: each channel pins to one Claude Code session id. First
  call uses `--session-id <new-uuid>`; later calls use `--resume`. We
  never use `--continue` — that's the race the original file-queue
  prototype had to serialize around.
* **Concurrency**: distinct channels run in parallel; same-channel calls
  serialize behind an `asyncio.Lock` so message ordering is preserved.
* **State**: channel→session map persisted atomically (temp + `os.replace`)
  to `~/.claude-bridge/sessions.json`.
* **Failure surface**: subprocess errors, timeouts, missing binary, bad
  JSON — all return structured `ok: false` results, never raise. The MCP
  layer turns raises into opaque `ToolError`s, so this matters.

## Security

* The container is the trust boundary. Anyone who can `docker exec` into
  it can drive `claude` with whatever auth lives there. Same goes for
  whoever can edit `claude_desktop_config.json` on the host.
* `acceptEdits` (the default) auto-accepts file edits but still prompts
  for `Bash`. Fine when the prompt doesn't need shell.
* `bypassPermissions` removes all gates including the workspace sandbox
  — only safe when the container's network/filesystem isolation is the
  thing keeping you safe. See [Running unattended](#running-unattended-max-permissions).

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest             # 13 tests, ~3s
```

Tests use a fake `claude` binary (a Python stub in `tests/conftest.py`)
so they verify real subprocess behavior — argv formation, JSON parsing,
exit codes, timeouts, env passthrough, per-call cwd — without a real
Anthropic API key.
