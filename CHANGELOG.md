# Changelog

All notable changes to **claude-bridge** are documented here. The format
is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project follows [Semantic Versioning](https://semver.org/) once
released.

## [Unreleased]

Currently the only release stream. Once `0.1.0` is published, this
section moves to a numbered entry below.

### Changed — PyPI distribution name
- **Renamed PyPI distribution from `claude-bridge` to `claude-bridge-mcp`**
  (the bare name was already taken on PyPI by an unrelated HTTP
  gateway). The Python import (`import claude_bridge`),
  `claude_bridge.__version__`, the CLI entry point (`claude-bridge`),
  and the MCP server identifier (`claude-bridge`) are all unchanged —
  only the `pip install <name>` argument differs. No code changes;
  existing `claude_desktop_config.json` entries keep working.

### Changed — Pre-0.1.0 audit fixes
- **Lock and persistence plumbing**: renamed `_save_jobs_sync` →
  `_save_jobs_unlocked` (and the schedules / events siblings) to make
  the "init-only or shielded" semantics explicit. Save locks
  (`_jobs_save_lock`, `_schedules_save_lock`, `_events_save_lock`) are
  now lazy `Optional[asyncio.Lock]` constructed inside the first async
  method that needs them, decoupling lock construction from
  module-import-time loop binding.
- **Concurrent-write race fix**: `sessions.json`, `jobs.json`,
  `schedules.json`, and `events.json` writes now use a unique tmp
  suffix (`<file>.tmp.<uuid>`) so two concurrent writers can't clobber
  each other's tempfile mid-write. Tmp files are unlinked on any
  failure path so disambiguator-suffixed corpses don't accumulate.
- **PID-recycling correctness**: the subprocess pgid is captured at
  spawn (when the PID is provably ours) and persisted on `Job`. Cancel
  / timeout / watcher paths target the stored pgid instead of calling
  `os.getpgid(pid)`, which races with PID recycling once the
  subprocess has exited.
- **SSRF guard on webhooks**: `notify_url` is validated at
  `dispatch_async` / `create_schedule` time. Non-`http(s)` schemes,
  unresolvable hosts, and addresses in private / loopback /
  link-local / multicast / reserved ranges are rejected with a clear
  error. Override via `CLAUDE_BRIDGE_ALLOW_PRIVATE_WEBHOOKS=1`.
- **`cancel_dispatch` reflects reality**: `cancelled` in the response
  now mirrors `Task.cancel()`'s actual return value, not a hardcoded
  `True`.
- **Cancel-handler durability**: `_tracked_dispatch`'s `CancelledError`
  branch persists via the unlocked sync writer and shields/suppresses
  the webhook send so interpreter-teardown can't truncate persistence.
- **`dispatch_async` error path**: a `RuntimeError` from
  `asyncio.create_task` (closed loop) now marks the job `error`,
  persists, logs, and re-raises so callers see a definite failure.
- **Supervisor backpressure**: the supervisor thread skips its tick if
  the previous `ensure_watchers_running` future is still pending, and
  any failure on that future is logged via `_log_event` instead of
  silently swallowed.
- **Eviction reclaims disk**: `_evict_completed_if_needed` now
  `shutil.rmtree`s the evicted job's `job-output/<job_id>/` so a
  long-lived bridge doesn't leak bytes for jobs no caller can look up.
- **Orphan finalize uses on-disk mtime**: `duration_ms` for recovered
  orphans is computed from the stdout file's mtime when available,
  falling back to wall-clock only on stat failure. Recovered jobs
  no longer report a 0ms duration when the bridge restarts hours
  after the subprocess actually exited.
- **`list_completions` performance**: filter on `Job.finished_at`
  before materializing entries via `get_dispatch`; sort + limit on
  cheap tuples and only build full entries for the survivors. Same
  shape applies to `wait_any_completion`.
- **Naive `until` datetimes treated as UTC**: previously they used
  local time (depending on the container TZ); now `_parse_until`
  promotes naive datetimes to UTC explicitly. Documented in
  `bridge_help` and the `schedule_dispatch` tool docstring.
- **Event-log debounce**: `_log_event` no longer fsyncs `events.json`
  on every call. Disk writes are coalesced via a 100ms timer and
  executor-offloaded; `_save_jobs` / `_save_schedules` opportunistically
  flush so terminal transitions still land in `events.json` before
  test/`shutdown()` paths read it back.
- **Defensive load**: `_load_jobs` / `_load_schedules` now log
  per-entry drops via `_log_event` (for post-mortem) and quarantine
  cycles in persisted schedule chains as `error` instead of letting
  the scheduler deadlock. `__post_init__` loads events first so these
  recovery events have a buffer to land in.
- **Configurable minimum schedule interval**: lifted
  `_min_schedule_interval_seconds` to a public dataclass field
  (`min_schedule_interval_seconds`) wired to
  `CLAUDE_BRIDGE_MIN_SCHEDULE_INTERVAL` (default `10`).
- **Top-level package exports**: `Job`, `Schedule`, `STOP_SENTINEL`
  are now re-exported from `claude_bridge` for typing / discovery.
- **Test hygiene**: autouse fixture tracks every `Dispatcher`
  constructed in a test and calls `shutdown()` on teardown so daemon
  threads don't leak. The loopback HTTP capture-server pattern is now
  a proper fixture (`capture_server`) with `server.shutdown()` /
  `server_close()` / `thread.join` teardown. Timing-sensitive bounds
  bumped to absorb CI-runner GC noise. The orphan-vs-cancel test
  split into two deterministic cases.
- **Packaging**: PEP 639 SPDX `license = "MIT"` + `license-files`,
  dropped the obsolete `License :: OSI Approved :: MIT License`
  classifier. Explicit `[tool.hatch.build.targets.sdist]` include
  list so PyPI uploads don't ship local scratch.

### Added — Core
- Synchronous `dispatch` MCP tool: run a prompt against `claude -p`
  inside a devcontainer; return a structured result with `ok`,
  `result`, `session_id`, `duration_ms`, `raw`, `stderr`.
- Per-channel session pinning. The first dispatch on a channel mints a
  fresh `--session-id`; subsequent dispatches `--resume` it. Distinct
  channels run in parallel; same-channel calls serialize behind an
  `asyncio.Lock`.
- Atomic state persistence (`sessions.json`) via temp + `os.replace`.
- Auth-agnostic shell-out: the bridge never touches credentials and
  inherits the container's environment unchanged so `ANTHROPIC_API_KEY`,
  `CLAUDE_CODE_OAUTH_TOKEN`, and on-disk `~/.claude/.credentials.json`
  all work without configuration.

### Added — Asynchronous dispatch
- `dispatch_async` / `get_dispatch` / `wait_dispatch` /
  `cancel_dispatch` / `list_jobs` for prompts that exceed the MCP
  transport's per-call ceiling.
- Subprocess decoupling: async dispatches spawn `claude -p` with
  `start_new_session=True`, stdin from `/dev/null`, stdout/stderr to
  files under `<state>.parent/job-output/<job_id>/`. Subprocesses
  survive transport timeouts and bridge crashes.
- Job persistence to `jobs.json` on every state transition.
- Bridge-restart recovery: in-flight jobs reattach to live PIDs via a
  watcher coroutine; jobs whose PIDs are gone but output files are
  intact are finalized from the on-disk JSON. Channels of fully-lost
  jobs are auto-unpinned to avoid races with stray subprocesses.
- Distinct cancellation taxonomy: `cancelled` (user via
  `cancel_dispatch`), `abandoned` (runtime/transport cancel — subprocess
  kept alive), `orphaned` (lost across restart).
- Per-call `cwd` override on `dispatch` and `dispatch_async`.

### Added — Recurring dispatches
- `schedule_dispatch` / `list_schedules` / `get_schedule` /
  `cancel_schedule`. Each tick is its own `dispatch_async` job.
- Schedule persistence (`schedules.json`); resumes after bridge restart
  without burst-firing missed ticks.
- `[BRIDGE_STOP_SCHEDULE]` self-cancellation sentinel: a tick whose
  result text contains the literal string cancels its own schedule.
- `after_schedule_id` chaining: a schedule starts in `waiting` and
  promotes to `active` automatically when its predecessor reaches a
  terminal state. Cycles rejected at creation.
- Minimum interval guardrail (default 10s) to prevent runaway tick
  storms.

### Added — Notifications and event log
- Optional outbound webhooks on `dispatch_async` and `schedule_dispatch`
  via `notify_url`, `notify_on`, `notify_headers`. Fire-and-log
  delivery using stdlib `urllib.request` (no new dependencies).
  Failures recorded as `webhook_failed` events; never propagated.
- `list_completions` / `wait_any_completion` for cursor-based polling
  of finished jobs.
- `list_events(since, limit, types, notable_only)` — bridge-wide
  structured event stream, persisted across restarts. `notable_only`
  filters to terminal transitions and failures, dropping per-tick /
  per-start chatter.

### Added — Discoverability
- `bridge_help()` MCP tool: structured map of every tool (with
  `when_to_use` / `when_not_to_use`), seven canonical workflows, nine
  concept definitions, durability guarantees, gotchas, and the curated
  `notable_event_types` set. Built so an agent can call `bridge_help`
  once and use the full surface correctly.
- Symmetry contract: every tool advertised by `bridge_help["tools"]`
  must appear in `mcp.list_tools()` and vice versa. Pinned by
  `test_mcp_tools_list_matches_help_index`.

### Configuration
- `CLAUDE_BRIDGE_STATE`, `CLAUDE_BRIDGE_CWD`, `CLAUDE_BRIDGE_CLAUDE_BIN`,
  `CLAUDE_BRIDGE_DEFAULT_PERMISSION_MODE`, `CLAUDE_BRIDGE_LOG`,
  `CLAUDE_BRIDGE_PERSIST_PROMPTS`, `CLAUDE_BRIDGE_LOG_PROMPTS`. All
  configuration is environment-only (never negotiated over MCP) so an
  untrusted MCP client can't rebase the state file.

### Tests
- 83 tests across `tests/test_dispatcher.py` and `tests/test_server.py`,
  including end-to-end webhook delivery via a real loopback HTTP server,
  bridge-restart recovery scenarios, and the discoverability symmetry
  contract.

[Unreleased]: https://github.com/JosiahSiegel/claude-bridge/commits/main
