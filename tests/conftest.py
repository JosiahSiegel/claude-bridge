"""Shared fixtures.

The dispatcher tests run a real subprocess but point ``claude_bin`` at a
small Python script that mimics ``claude -p --output-format json``. Going
through a real subprocess catches things a stub would miss (argv parsing,
JSON I/O, exit codes, timeouts).
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest


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
