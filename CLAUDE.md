# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

An MCP server (`claude-bridge`) that runs inside a devcontainer and lets host-side Claude Cowork dispatch work into the containerized `claude -p` over `docker exec -i` stdio. Replaces an earlier file-queue prototype. Python ≥ 3.11, single runtime dependency on `mcp` (FastMCP), shells out to the `claude` CLI rather than using the Agent SDK so the bridge inherits whatever auth the user already configured.

## Common commands

```bash
# install (editable) and run tests
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest                      # 12 tests, ~3s
.venv/bin/pytest tests/test_dispatcher.py::test_auth_env_passes_through_to_subprocess -v

# run the MCP server interactively (stdio; for debugging via something like
# the MCP inspector, not for normal operation)
.venv/bin/claude-bridge

# sanity-check that the underlying claude works (the bridge will too if this does)
claude -p "hi" --output-format json
```

There is no lint config and no build step beyond `pip install -e`.

## Architecture

Two files do all the work:

1. **`src/claude_bridge/dispatcher.py`** — `Dispatcher` class. Spawns `claude -p` via `asyncio.create_subprocess_exec`, returns a `DispatchResult` dataclass. Holds the channel→session-id map, persists it atomically to `$HOME/.claude-bridge/sessions.json`. Per-channel `asyncio.Lock` so calls within a channel serialize, but distinct channels run concurrently.
2. **`src/claude_bridge/server.py`** — FastMCP server. Three `@mcp.tool` definitions (`dispatch`, `list_channels`, `reset_channel`) plus a `main()` that runs the stdio transport. All configuration is env-var-driven (no MCP-side knobs) so the host can't accidentally redirect the state file or change the working directory.

Test plumbing in `tests/conftest.py` writes a small Python script to a tmpdir that mimics `claude -p --output-format json` — tests run real subprocesses against it, which catches argv/JSON/exit-code regressions a stub couldn't.

## Load-bearing invariants

These are the things that will silently break the product if you change them without thinking:

1. **Never use `--continue`.** Always use `--session-id <new-uuid>` for the first call on a channel and `--resume <session_id>` thereafter. `--continue` means "most recent session in cwd" and races with concurrent dispatches. The old file-queue had to serialize *everything* because of this; the bridge doesn't, *because* of the per-channel pinning. Removing it would silently re-introduce the race.
2. **Don't pass `env=` to `create_subprocess_exec`.** The bridge must inherit the container's environment unchanged so any of `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, or on-disk `~/.claude/.credentials.json` works without the user telling the bridge which one. There's a test that pins this — `test_auth_env_passes_through_to_subprocess`. If you need to inject env, layer it; don't replace.
3. **Failures must return a `DispatchResult` with `ok=False`, never raise.** The MCP layer surfaces these to Cowork as tool results. A raised exception becomes an opaque `ToolError` on the host side. Subprocess failure, timeout, missing binary, bad JSON — all five paths return a structured error.
4. **Atomic state writes.** `_save_state` writes to `<state>.tmp` and `os.replace`s. Don't switch to a plain write — a crash mid-write would corrupt the channel→session map.
5. **Configuration is env-only, never via MCP arguments.** The MCP client (Cowork) is untrusted in the sense that any prompt it sends could try to influence behavior. The state file path, cwd, and claude binary location must be set in the *container's* env at startup. Don't add a tool that takes `cwd=` or `state_path=`.

## What lives in tests

`tests/test_dispatcher.py` covers each of the invariants above plus the channel concurrency model. The fake-claude binary in `conftest.py` accepts `CLAUDE_FAKE_*` env vars to simulate exit codes, sleep, stderr, and required-env-presence — that's how `test_auth_env_passes_through_to_subprocess` proves env is inherited (the fake exits 99 if any of `ANTHROPIC_API_KEY`/`CLAUDE_CODE_OAUTH_TOKEN`/`HOME` is missing in the subprocess).

## Auth — what works

This is the one place where being generic actually mattered. The bridge supports any `claude` auth mode because it never touches credentials itself — it just runs `claude -p` and lets the CLI decide:

* `ANTHROPIC_API_KEY` env var
* `CLAUDE_CODE_OAUTH_TOKEN` (long-lived token from `claude setup-token`)
* On-disk `~/.claude/.credentials.json` from `claude /login` (claude.ai subscription)

Unlike Remote Control, which rejects API keys and inference-only OAuth, the bridge has no auth opinion. README documents this; the test enforces it.

## Where this came from

There's git history showing an earlier `watch.sh` / `watch.py` / `send.sh` file-queue prototype. The rewrite was driven by: (a) Cowork sees a real tool surface instead of a folder convention, (b) eliminate the `--continue` race so concurrent channels work, (c) remove polling. If those constraints ever change, the file-queue is preserved in git history as a fallback design.
