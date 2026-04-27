"""Shared fixtures.

The dispatcher tests run a real subprocess but point ``claude_bin`` at a
small Python script that mimics ``claude -p --output-format json``. Going
through a real subprocess catches things a stub would miss (argv parsing,
JSON I/O, exit codes, timeouts).
"""

from __future__ import annotations

import gc
import stat
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import claude_bridge.dispatcher as _dispatcher_module

if TYPE_CHECKING:
    import http.server
    import threading

    from claude_bridge.dispatcher import Dispatcher


# Behavior is controlled by env vars set by individual tests:
#   CLAUDE_FAKE_RESULT       — the .result string (default: 'hi')
#   CLAUDE_FAKE_EXIT         — exit code (default: 0)
#   CLAUDE_FAKE_STDERR       — stderr contents
#   CLAUDE_FAKE_SLEEP        — seconds to sleep before exiting (for timeouts)
#   CLAUDE_FAKE_LOG          — append-line log path; we record the full argv
#                              so the test can assert on flags like --resume
#   CLAUDE_FAKE_REQUIRE_ENV  — colon-separated list of env vars that MUST be
#                              set; if any is missing, exit 99 with stderr.
#                              Used to assert auth env passthrough.
#   CLAUDE_FAKE_CWD_LOG      — append-line log path; we record os.getcwd()
#                              so the test can assert per-call cwd was honored.
FAKE_CLAUDE_BODY = '''\
import json
import os
import sys
import time
import uuid

log_path = os.environ.get("CLAUDE_FAKE_LOG")
if log_path:
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(" ".join(sys.argv[1:]) + "\\n")

cwd_log = os.environ.get("CLAUDE_FAKE_CWD_LOG")
if cwd_log:
    with open(cwd_log, "a", encoding="utf-8") as f:
        f.write(os.getcwd() + "\\n")

required = os.environ.get("CLAUDE_FAKE_REQUIRE_ENV", "")
if required:
    missing = [k for k in required.split(":") if k and not os.environ.get(k)]
    if missing:
        print("missing env: " + ",".join(missing), file=sys.stderr)
        sys.exit(99)

sleep_s = float(os.environ.get("CLAUDE_FAKE_SLEEP", "0") or "0")
if sleep_s:
    time.sleep(sleep_s)

stderr = os.environ.get("CLAUDE_FAKE_STDERR", "")
if stderr:
    print(stderr, file=sys.stderr)

exit_code = int(os.environ.get("CLAUDE_FAKE_EXIT", "0") or "0")

argv = sys.argv[1:]
session_id = None
for i, arg in enumerate(argv):
    if arg in ("--session-id", "--resume") and i + 1 < len(argv):
        session_id = argv[i + 1]
        break
if not session_id:
    session_id = str(uuid.uuid4())

if exit_code == 0:
    payload = {
        "result": os.environ.get("CLAUDE_FAKE_RESULT", "hi"),
        "session_id": session_id,
        "is_error": False,
    }
    print(json.dumps(payload))

sys.exit(exit_code)
'''


@pytest.fixture
def fake_claude(tmp_path: Path) -> Path:
    """Return a path to an executable script that mimics ``claude -p``."""
    script = tmp_path / "fake-claude"
    script.write_text(
        f"#!{sys.executable}\n{FAKE_CLAUDE_BODY}",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    return tmp_path / "state.json"


@pytest.fixture
def argv_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Tail this file to assert which flags claude was invoked with."""
    p = tmp_path / "argv.log"
    monkeypatch.setenv("CLAUDE_FAKE_LOG", str(p))
    return p


@pytest.fixture(autouse=True)
def _clear_fake_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "CLAUDE_FAKE_RESULT",
        "CLAUDE_FAKE_EXIT",
        "CLAUDE_FAKE_STDERR",
        "CLAUDE_FAKE_SLEEP",
        "CLAUDE_FAKE_REQUIRE_ENV",
        "CLAUDE_FAKE_CWD_LOG",
    ):
        monkeypatch.delenv(key, raising=False)


# ---------- dispatcher / loopback-server cleanup ----------
#
# Tests construct ``Dispatcher`` instances directly, which start a daemon
# supervisor thread on first ``ensure_watchers_running`` and (in webhook
# tests) a loopback HTTPServer thread. Without explicit teardown those
# leak across the suite — the supervisor thread is daemonized so the
# process exits, but during a test run they accumulate and complicate
# diagnostics.
#
# The autouse cleanup fixture below patches ``Dispatcher.__post_init__``
# to register every instance, then calls ``.shutdown()`` on each at
# teardown. The ``capture_server`` fixture wraps the loopback HTTPServer
# pattern and tears it down properly.


_active_dispatchers: list[Dispatcher] = []


@pytest.fixture(autouse=True)
def _cleanup_dispatchers():
    """Track ``Dispatcher`` instances created during a test and call
    ``.shutdown()`` on each at teardown so daemon threads don't leak."""
    _active_dispatchers.clear()
    original_init = _dispatcher_module.Dispatcher.__post_init__

    def _tracking_post_init(self) -> None:
        original_init(self)
        _active_dispatchers.append(self)

    _dispatcher_module.Dispatcher.__post_init__ = _tracking_post_init
    try:
        yield
    finally:
        _dispatcher_module.Dispatcher.__post_init__ = original_init
        for d in list(_active_dispatchers):
            try:
                d.shutdown()
            except Exception:  # noqa: BLE001 — never let cleanup fail a test
                pass
        _active_dispatchers.clear()
        gc.collect()


@pytest.fixture
def capture_server():
    """Spin up a loopback HTTP server that captures POST bodies, hand
    out a ``(url, received_list)`` factory, and tear the server down at
    test end so we don't leak threads.

    Usage::

        async def test_x(capture_server):
            received = []
            url = capture_server(received)
            ...
    """
    import http.server
    import json as _json
    import threading

    servers: list[tuple[http.server.HTTPServer, threading.Thread]] = []

    def _make(received: list[dict]) -> str:
        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length).decode("utf-8")
                try:
                    received.append(_json.loads(body))
                except _json.JSONDecodeError:
                    received.append({"_raw": body})
                self.send_response(204)
                self.end_headers()

            def log_message(self, format, *args):  # noqa: A002
                return

        server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        port = server.server_address[1]
        return f"http://127.0.0.1:{port}/"

    try:
        yield _make
    finally:
        for server, thread in servers:
            try:
                server.shutdown()
            except Exception:  # noqa: BLE001
                pass
            try:
                server.server_close()
            except Exception:  # noqa: BLE001
                pass
            try:
                thread.join(timeout=2.0)
            except Exception:  # noqa: BLE001
                pass
