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

```
dispatch(
  prompt="audit the auth middleware",
  channel="auth-audit",
  cwd="/workspace",          # the project you want claude to work in
  timeout_seconds=180
)
```

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

## Tools

### `dispatch(prompt, channel="default", timeout_seconds=300, permission_mode=None, cwd=None)`

Run a prompt against `claude -p` inside the container. Channels pin to
one Claude Code session each — first call starts fresh, subsequent calls
on the same channel `--resume` it. Distinct channels run in parallel;
same-channel calls serialize.

* `permission_mode`: `default`, `acceptEdits`, `plan`, or
  `bypassPermissions`. Defaults to `acceptEdits` (override via
  `CLAUDE_BRIDGE_DEFAULT_PERMISSION_MODE`).
* `cwd`: per-call override of the working directory. Use it to retarget
  a single dispatch at a different repo without standing up a second
  bridge.

Returns `{ok, channel, duration_ms, result, session_id, raw}` on
success, or `{ok: false, channel, duration_ms, error, exit_code}` on
failure. **Failures never raise** — the MCP layer always sees a result.

### `list_channels()`

`{"channels": {channel: session_id, ...}}`. Doesn't invoke `claude`.

### `reset_channel(channel)`

Drops a channel's pinned session so the next dispatch starts fresh.
`{"reset": true|false, "channel": ...}`.

## Configuration (env vars in the container)

| Variable | Default | Purpose |
|---|---|---|
| `CLAUDE_BRIDGE_STATE` | `~/.claude-bridge/sessions.json` | Channel→session map (atomic writes) |
| `CLAUDE_BRIDGE_CWD` | bridge process cwd | **Default** working dir for `claude -p`. Per-call `cwd=` overrides |
| `CLAUDE_BRIDGE_CLAUDE_BIN` | `claude` | Override `claude` binary location |
| `CLAUDE_BRIDGE_DEFAULT_PERMISSION_MODE` | `acceptEdits` | Default if caller omits `permission_mode` |

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
  it can drive `claude` with whatever auth lives there.
* `acceptEdits` (the default) auto-accepts file edits but still prompts
  for `Bash` and other tools — fine for headless operation if the
  prompt doesn't need shell. `bypassPermissions` only when the
  container's network/filesystem isolation is doing the work; see the
  [official devcontainer guide](https://code.claude.com/docs/en/devcontainer).

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
