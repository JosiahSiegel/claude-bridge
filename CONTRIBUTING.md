# Contributing to claude-bridge

Thanks for considering a contribution. This file is the short
operational guide; for *why* the design is the way it is, read
[`CLAUDE.md`](./CLAUDE.md) — it lists the load-bearing invariants any
change must preserve.

## Dev setup

```bash
git clone https://github.com/JosiahSiegel/claude-bridge.git
cd claude-bridge
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest             # 83 tests, ~20s
```

The bridge is a runtime dependency for an MCP client (typically Claude
Desktop / Cowork) that runs *outside* the bridge's Python environment.
For local hacking you don't need an MCP client — `pytest` exercises
the dispatcher and the MCP shim directly with a fake `claude` binary.

## The editable-install gotcha

The `claude-bridge` script that ends up on your `$PATH` after
`pip install -e .` lives in the venv's `bin/`. `docker exec` from a
host MCP client does **not** activate venvs and does **not** source
`~/.bashrc`, so the bare command name is often invisible. When wiring
this up for real (e.g. in a `claude_desktop_config.json` entry), use
the absolute path that `which claude-bridge` prints inside the
container. The `README.md` covers this in detail under "Quickstart".

## Run the tests

```bash
.venv/bin/pytest                    # all
.venv/bin/pytest tests/test_dispatcher.py::test_first_dispatch_pins_session_id -v
.venv/bin/pytest -k schedule        # everything matching "schedule"
```

The fake-claude binary is a Python stub written into a tmpdir per
test (see `tests/conftest.py`). It accepts env vars
(`CLAUDE_FAKE_SLEEP`, `CLAUDE_FAKE_EXIT`, `CLAUDE_FAKE_RESULT`,
`CLAUDE_FAKE_STDERR`, `CLAUDE_FAKE_LOG`, `CLAUDE_FAKE_CWD_LOG`,
`CLAUDE_FAKE_REQUIRE_ENV`) so async / cancellation / cwd / env-passthrough
behavior can be tested with real subprocess semantics, no API key
required.

## Conventions

- **Don't break the load-bearing invariants in `CLAUDE.md`.** Each one
  is pinned by a test for a reason — the comment block in CLAUDE.md
  explains the why, and the test name in `tests/test_dispatcher.py`
  explains the what.
- **Failures must return a `DispatchResult` with `ok=False`, never
  raise out of the MCP layer.** The transport surfaces raises as
  opaque `ToolError`s; structured failure dicts are the contract.
- **Configuration via env vars only — never via MCP arguments.** The
  exception is per-call `cwd=` on `dispatch` / `dispatch_async`; the
  bridge-wide *default* is still env-driven.
- **Every new tool that's added to `server.py` must also appear in
  `bridge_help`.** The symmetry test
  (`test_mcp_tools_list_matches_help_index`) will fail otherwise.
- **Async surface returns structured results, sync raises only on
  programmer error.** This isn't a hard rule but it matches existing
  patterns.

## PRs

- Run `pytest` locally before submitting; CI runs the same matrix on
  Python 3.11–3.13 (see `.github/workflows/test.yml`).
- Update `CHANGELOG.md` under `[Unreleased]` with a one-liner.
- If you add a tool, env var, or workflow pattern, update both
  `README.md` and `bridge_help()` in `server.py`.

## Security

See [`SECURITY.md`](./SECURITY.md). The container is the trust
boundary; anyone who can `docker exec` into it can drive `claude` with
whatever auth lives there.
