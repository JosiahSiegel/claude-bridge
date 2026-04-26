"""Behavior tests for the dispatcher.

These verify the load-bearing invariants:

* First dispatch on a channel goes out with ``--session-id <uuid>`` (no
  ``--resume``, no ``--continue``); subsequent dispatches use ``--resume``.
* Session ids persist to disk and survive Dispatcher reconstruction.
* Different channels are isolated (their session ids don't bleed).
* Failures (nonzero exit, timeouts, missing binary, bad JSON) yield
  structured error dicts rather than raising.
* ``reset_channel`` drops the pinning so the next call starts fresh.
* The async dispatch surface (``dispatch_async`` / ``get_dispatch`` /
  ``wait_dispatch`` / ``cancel_dispatch``) returns immediately, lets the
  caller poll, kills subprocesses on cancel, and respects channel locks.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from claude_bridge.dispatcher import Dispatcher


def _argv_lines(p: Path) -> list[list[str]]:
    if not p.exists():
        return []
    return [line.split() for line in p.read_text().splitlines() if line.strip()]


async def test_first_dispatch_pins_session_id(fake_claude, state_path, argv_log):
    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    res = await d.dispatch("hello", channel="alpha")
    assert res.ok, res.error
    assert res.session_id, "expected a session id"
    assert d.list_channels() == {"alpha": res.session_id}

    [argv] = _argv_lines(argv_log)
    assert "--session-id" in argv
    assert "--resume" not in argv
    sid_idx = argv.index("--session-id")
    assert argv[sid_idx + 1] == res.session_id


async def test_second_dispatch_uses_resume(fake_claude, state_path, argv_log):
    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    first = await d.dispatch("hi", channel="alpha")
    second = await d.dispatch("again", channel="alpha")
    assert second.ok
    assert second.session_id == first.session_id

    lines = _argv_lines(argv_log)
    assert len(lines) == 2
    assert "--resume" in lines[1]
    resume_idx = lines[1].index("--resume")
    assert lines[1][resume_idx + 1] == first.session_id


async def test_channels_are_isolated(fake_claude, state_path):
    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    a = await d.dispatch("hi", channel="alpha")
    b = await d.dispatch("hi", channel="beta")
    assert a.session_id != b.session_id
    assert d.list_channels() == {"alpha": a.session_id, "beta": b.session_id}


async def test_state_persists_across_reconstruction(fake_claude, state_path):
    d1 = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    first = await d1.dispatch("hi", channel="alpha")

    d2 = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    assert d2.list_channels() == {"alpha": first.session_id}


async def test_reset_channel_drops_pinning(fake_claude, state_path, argv_log):
    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    first = await d.dispatch("hi", channel="alpha")
    assert d.reset_channel("alpha") is True
    assert d.list_channels() == {}
    assert d.reset_channel("alpha") is False  # idempotent

    second = await d.dispatch("hi again", channel="alpha")
    assert second.session_id != first.session_id

    lines = _argv_lines(argv_log)
    assert "--session-id" in lines[-1]  # fresh start, not --resume


async def test_nonzero_exit_returns_error(fake_claude, state_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_FAKE_EXIT", "7")
    monkeypatch.setenv("CLAUDE_FAKE_STDERR", "auth expired")

    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    res = await d.dispatch("hi", channel="alpha")
    assert not res.ok
    assert res.exit_code == 7
    assert "auth expired" in (res.error or "")


async def test_timeout_kills_subprocess(fake_claude, state_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_FAKE_SLEEP", "5")

    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    res = await d.dispatch("hi", channel="alpha", timeout_seconds=1)
    assert not res.ok
    assert "timeout" in (res.error or "")


async def test_missing_binary_returns_error(state_path):
    d = Dispatcher(state_path=state_path, claude_bin="/no/such/binary")
    res = await d.dispatch("hi", channel="alpha")
    assert not res.ok
    assert "PATH" in (res.error or "") or "not on" in (res.error or "")


async def test_empty_prompt_rejected(fake_claude, state_path):
    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    res = await d.dispatch("   ", channel="alpha")
    assert not res.ok
    assert "empty" in (res.error or "")


async def test_concurrent_different_channels_run_in_parallel(
    fake_claude, state_path, monkeypatch
):
    """Different channels must not serialize each other."""
    monkeypatch.setenv("CLAUDE_FAKE_SLEEP", "0.5")

    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    start = time.monotonic()
    a, b = await asyncio.gather(
        d.dispatch("x", channel="alpha"),
        d.dispatch("y", channel="beta"),
    )
    elapsed = time.monotonic() - start

    assert a.ok and b.ok
    # Two 0.5s sleeps in parallel should finish in well under 1s if
    # truly concurrent. Generous bound to avoid CI flakiness.
    assert elapsed < 0.95, f"channels serialized; elapsed={elapsed:.2f}s"


async def test_auth_env_passes_through_to_subprocess(
    fake_claude, state_path, monkeypatch
):
    """The container's claude auth env (API key, OAuth token, etc.) must
    reach the spawned ``claude -p`` process unchanged. The dispatcher must
    not strip or override these — claude itself decides which auth mode to
    use based on what's present.
    """
    # All three of these are auth-mode-relevant env vars that real users
    # might have set. ``CLAUDE_FAKE_REQUIRE_ENV`` makes the fake binary
    # exit 99 with a "missing env" stderr if any is unset, so the
    # subprocess only succeeds if the parent env propagated.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-test")
    monkeypatch.setenv("HOME", str(state_path.parent))  # for ~/.claude lookup
    monkeypatch.setenv(
        "CLAUDE_FAKE_REQUIRE_ENV",
        "ANTHROPIC_API_KEY:CLAUDE_CODE_OAUTH_TOKEN:HOME",
    )

    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    res = await d.dispatch("hi", channel="alpha")
    assert res.ok, f"env did not propagate: {res.error}"


async def test_per_call_cwd_overrides_default(
    fake_claude, state_path, tmp_path, monkeypatch
):
    """A per-call ``cwd`` argument must override ``default_cwd`` for that
    invocation only. Lets a caller anchor the bridge in a clean directory
    for fast cold starts but retarget individual dispatches at a busier
    project (e.g. ``cwd="/workspace"``) without a second bridge instance.
    """
    cwd_log = tmp_path / "cwd.log"
    monkeypatch.setenv("CLAUDE_FAKE_CWD_LOG", str(cwd_log))

    default_dir = tmp_path / "bridge-home"
    override_dir = tmp_path / "project"
    default_dir.mkdir()
    override_dir.mkdir()

    d = Dispatcher(
        state_path=state_path,
        claude_bin=str(fake_claude),
        default_cwd=str(default_dir),
    )

    # First call: no cwd → falls back to default_cwd.
    a = await d.dispatch("hi", channel="alpha")
    assert a.ok
    # Second call: explicit cwd → overrides default for this call only.
    b = await d.dispatch("hi", channel="beta", cwd=str(override_dir))
    assert b.ok
    # Third call: no cwd again → back to default.
    c = await d.dispatch("hi", channel="gamma")
    assert c.ok

    cwds = [line for line in cwd_log.read_text().splitlines() if line.strip()]
    assert cwds == [str(default_dir), str(override_dir), str(default_dir)], cwds


# ---------- async dispatch surface ----------


async def test_dispatch_async_returns_job_id_immediately(
    fake_claude, state_path, monkeypatch
):
    """dispatch_async must not block on the underlying claude run."""
    monkeypatch.setenv("CLAUDE_FAKE_SLEEP", "1")
    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))

    start = time.monotonic()
    job_id = await d.dispatch_async("hi", channel="alpha")
    elapsed = time.monotonic() - start

    assert isinstance(job_id, str) and len(job_id) >= 8
    assert elapsed < 0.5, f"dispatch_async blocked for {elapsed:.2f}s"
    assert d.get_dispatch(job_id)["status"] == "running"

    # Drain so we don't leak the task into the next test.
    final = await d.wait_dispatch(job_id, max_wait_seconds=5)
    assert final["status"] == "done", final


async def test_get_dispatch_running_then_done(
    fake_claude, state_path, monkeypatch
):
    monkeypatch.setenv("CLAUDE_FAKE_SLEEP", "0.5")
    monkeypatch.setenv("CLAUDE_FAKE_RESULT", "hello back")
    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))

    job_id = await d.dispatch_async("hi", channel="alpha")
    assert d.get_dispatch(job_id)["status"] == "running"

    final = await d.wait_dispatch(job_id, max_wait_seconds=5)
    assert final["status"] == "done"
    assert final["ok"] is True
    assert final["result"] == "hello back"
    assert final["job_id"] == job_id
    assert final["channel"] == "alpha"


async def test_wait_dispatch_returns_running_on_timeout(
    fake_claude, state_path, monkeypatch
):
    """If the job outlives max_wait_seconds, status is still 'running'."""
    monkeypatch.setenv("CLAUDE_FAKE_SLEEP", "2")
    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))

    job_id = await d.dispatch_async("hi", channel="alpha")
    res = await d.wait_dispatch(job_id, max_wait_seconds=0.2)
    assert res["status"] == "running"

    # Underlying job survives the poller's timeout (asyncio.shield).
    final = await d.wait_dispatch(job_id, max_wait_seconds=5)
    assert final["status"] == "done"


async def test_get_dispatch_unknown_job_id_returns_error(state_path):
    d = Dispatcher(state_path=state_path)
    res = d.get_dispatch("not-a-real-id")
    assert res == {"ok": False, "error": "unknown job_id: not-a-real-id"}


async def test_cancel_dispatch_kills_subprocess(
    fake_claude, state_path, monkeypatch
):
    """Cancellation must kill claude -p, not orphan it."""
    monkeypatch.setenv("CLAUDE_FAKE_SLEEP", "10")
    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))

    job_id = await d.dispatch_async("hi", channel="alpha", timeout_seconds=30)
    # Give the task a moment to actually spawn the subprocess.
    await asyncio.sleep(0.1)

    cancel_res = d.cancel_dispatch(job_id)
    assert cancel_res["cancelled"] is True

    # Wait for the cancellation to propagate.
    final = await d.wait_dispatch(job_id, max_wait_seconds=2)
    assert final["status"] == "cancelled"
    assert final["ok"] is False


async def test_cancel_dispatch_already_finished_is_idempotent(
    fake_claude, state_path
):
    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    job_id = await d.dispatch_async("hi", channel="alpha")
    final = await d.wait_dispatch(job_id, max_wait_seconds=5)
    assert final["status"] == "done"

    cancel_res = d.cancel_dispatch(job_id)
    assert cancel_res["cancelled"] is False
    assert cancel_res["reason"] == "already_finished"


async def test_dispatch_async_serializes_per_channel(
    fake_claude, state_path, monkeypatch
):
    """Two async dispatches on the same channel must run sequentially."""
    monkeypatch.setenv("CLAUDE_FAKE_SLEEP", "0.5")
    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))

    start = time.monotonic()
    j1 = await d.dispatch_async("a", channel="alpha")
    j2 = await d.dispatch_async("b", channel="alpha")
    r1 = await d.wait_dispatch(j1, max_wait_seconds=5)
    r2 = await d.wait_dispatch(j2, max_wait_seconds=5)
    elapsed = time.monotonic() - start

    assert r1["status"] == "done" and r2["status"] == "done"
    # Both share the channel session id.
    assert r1["session_id"] == r2["session_id"]
    # Two 0.5s sleeps serialized take >= ~1s.
    assert elapsed >= 0.95, f"same-channel async ran in parallel; {elapsed:.2f}s"


async def test_dispatch_async_distinct_channels_run_in_parallel(
    fake_claude, state_path, monkeypatch
):
    monkeypatch.setenv("CLAUDE_FAKE_SLEEP", "0.5")
    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))

    start = time.monotonic()
    j1 = await d.dispatch_async("a", channel="alpha")
    j2 = await d.dispatch_async("b", channel="beta")
    r1 = await d.wait_dispatch(j1, max_wait_seconds=5)
    r2 = await d.wait_dispatch(j2, max_wait_seconds=5)
    elapsed = time.monotonic() - start

    assert r1["status"] == "done" and r2["status"] == "done"
    assert r1["session_id"] != r2["session_id"]
    assert elapsed < 0.95, f"distinct async channels serialized; {elapsed:.2f}s"


async def test_async_dispatch_pins_session_id_for_channel(
    fake_claude, state_path
):
    """The async path must update the channel→session map just like sync."""
    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    job_id = await d.dispatch_async("hi", channel="alpha")
    final = await d.wait_dispatch(job_id, max_wait_seconds=5)
    assert final["status"] == "done"
    assert d.list_channels()["alpha"] == final["session_id"]


async def test_dispatch_async_empty_prompt_raises(state_path, fake_claude):
    """Empty prompts surface synchronously — no orphan job."""
    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    try:
        await d.dispatch_async("   ", channel="alpha")
    except ValueError as exc:
        assert "empty" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError for empty prompt")
    assert d.list_jobs() == []


async def test_list_jobs_reports_running_and_done(
    fake_claude, state_path, monkeypatch
):
    monkeypatch.setenv("CLAUDE_FAKE_SLEEP", "0.5")
    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))

    finished = await d.dispatch_async("a", channel="alpha")
    await d.wait_dispatch(finished, max_wait_seconds=5)
    running = await d.dispatch_async("b", channel="beta")

    statuses = {j["job_id"]: j["status"] for j in d.list_jobs()}
    assert statuses[finished] == "done"
    assert statuses[running] == "running"

    # Drain.
    await d.wait_dispatch(running, max_wait_seconds=5)


async def test_completed_jobs_evicted_when_over_cap(
    fake_claude, state_path
):
    """Old finished jobs must be pruned to keep the table bounded."""
    d = Dispatcher(
        state_path=state_path,
        claude_bin=str(fake_claude),
        max_completed_jobs=2,
    )

    job_ids = []
    for i in range(5):
        jid = await d.dispatch_async(f"hi {i}", channel=f"ch{i}")
        await d.wait_dispatch(jid, max_wait_seconds=5)
        job_ids.append(jid)

    remaining = {j["job_id"] for j in d.list_jobs()}
    # Cap of 2 plus the one being inserted = at most 3 visible at any moment;
    # after the loop, the table should be back at the cap.
    assert len(remaining) <= 2, remaining
    # The two most recent must survive; older ones evicted.
    assert job_ids[-1] in remaining
    assert job_ids[0] not in remaining


async def test_concurrent_same_channel_serializes(
    fake_claude, state_path, monkeypatch
):
    """Same channel must serialize so --resume always sees the prior session."""
    monkeypatch.setenv("CLAUDE_FAKE_SLEEP", "0.5")

    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    start = time.monotonic()
    a, b = await asyncio.gather(
        d.dispatch("x", channel="alpha"),
        d.dispatch("y", channel="alpha"),
    )
    elapsed = time.monotonic() - start

    assert a.ok and b.ok
    assert a.session_id == b.session_id
    # Two 0.5s sleeps serialized should take >= ~1s.
    assert elapsed >= 0.95, f"same channel ran in parallel; elapsed={elapsed:.2f}s"
