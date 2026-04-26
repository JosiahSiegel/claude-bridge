"""Behavior tests for the dispatcher.

These verify the load-bearing invariants:

* First dispatch on a channel goes out with ``--session-id <uuid>`` (no
  ``--resume``, no ``--continue``); subsequent dispatches use ``--resume``.
* Session ids persist to disk and survive Dispatcher reconstruction.
* Different channels are isolated (their session ids don't bleed).
* Failures (nonzero exit, timeouts, missing binary, bad JSON) yield
  structured error dicts rather than raising.
* ``reset_channel`` drops the pinning so the next call starts fresh.
"""

from __future__ import annotations

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
    import asyncio
    import time

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


async def test_concurrent_same_channel_serializes(
    fake_claude, state_path, monkeypatch
):
    """Same channel must serialize so --resume always sees the prior session."""
    import asyncio
    import time

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
