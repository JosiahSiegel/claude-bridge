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

See [Tools → Polling pattern](#polling-pattern-the-canonical-long-running-flow)
for the full pattern.

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

The tool surface comes in two halves. Use the synchronous `dispatch`
when the round trip will fit comfortably under the MCP transport's
per-call ceiling (~60s in Cowork). Use the async surface for anything
longer — persona runs, multi-step refactors, work in a project with
heavy `.claude/` config.

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

#### `dispatch_async(prompt, channel="default", timeout_seconds=300, permission_mode=None, cwd=None)`

Kick off a dispatch in the background; return a `job_id` immediately.
Channel locking still applies — concurrent `dispatch_async` on the same
channel queue up. Empty prompts surface as `{"ok": false, "error": ...}`
synchronously (no orphan job).

Returns `{ok: true, job_id, channel}`.

#### `get_dispatch(job_id)`

Non-blocking status read. `status` is one of:

* `running` — also includes `elapsed_ms` so you can show progress.
* `done` — full sync-style result keys (`ok`, `result`, `session_id`,
  `duration_ms`, `raw`, `stderr`, …) plus `job_id` and `started_at`.
* `cancelled` — caller cancelled and the subprocess was killed.
* `error` — programmer error in the dispatcher itself; should be rare.
* `orphaned` — bridge process was restarted while the job was running;
  result was not captured. The job's channel pinning is auto-reset so
  the next dispatch on that channel starts fresh.

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
restarts, and Cowork's own MCP-transport quirks. The contract:

* **Channel→session pinning persists** to `sessions.json` via atomic
  temp+rename writes. A crash mid-write never corrupts it.
* **Every job's state persists** to `jobs.json` (same directory) on
  every transition: spawn, completion (success/failure), cancellation.
  After a bridge restart, `get_dispatch(job_id)` still returns the
  recorded result of any job that finished before the crash.
* **In-flight jobs at restart are marked `orphaned`**, not dropped.
  Callers see a definite terminal state instead of "unknown job_id".
  The orphaned job's channel pinning is **auto-reset** so the next
  dispatch on that channel starts a fresh session — avoiding races with
  any stray `claude -p` still running in the container that's holding
  the old session id.
* **Concurrent persistence is serialized** through an `asyncio.Lock`
  around the jobs-file write, so two parallel `dispatch_async` calls
  can't lose each other's updates.
* **Failures never raise out of the MCP layer.** Subprocess errors,
  timeouts, missing binary, bad JSON, cancellation — all return a
  structured `ok: false` result. The MCP transport always sees a clean
  tool response.
* **Cancellation kills the subprocess.** Calling `cancel_dispatch` on a
  running job triggers a `CancelledError` handler that `kill()`s
  `claude -p` before propagating, so cancellation never leaves an
  orphan worker spending API credits.
* **`wait_dispatch` is shielded.** If Cowork's MCP call to
  `wait_dispatch` gets aborted at the transport ceiling, the underlying
  job survives — Cowork just calls `wait_dispatch(job_id, ...)` again
  with the same `job_id`.

What this does **not** cover (and how to handle it):

* **Orphan `claude -p` processes** after a crash. The bridge knows the
  job is orphaned, but the actual subprocess might still be running in
  the container (parented to PID 1). It will exit on its own, usually
  in a few minutes. If you want to be aggressive: `pkill -f claude` in
  the container after a known restart. The auto-channel-reset means
  the next dispatch won't race it on the same session.
* **Container death.** If the container itself dies, all subprocesses
  die with it; nothing for the bridge to recover.

## Configuration (env vars in the container)

| Variable | Default | Purpose |
|---|---|---|
| `CLAUDE_BRIDGE_STATE` | `~/.claude-bridge/sessions.json` | Channel→session map (atomic writes). Job records persist alongside in `jobs.json` in the same directory |
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
