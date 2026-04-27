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
* Job state is persisted to disk so a bridge process restart still lets
  callers retrieve completed results, and in-flight jobs become
  ``orphaned`` (with their channel pinning auto-reset to avoid races
  with any orphan ``claude -p`` still running in the container).
* Stderr is captured even on success so callers can see project-MCP
  warnings.
* The optional event log (``CLAUDE_BRIDGE_LOG``) records state
  transitions and respects the prompt-redaction default.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from claude_bridge.dispatcher import STOP_SENTINEL, Dispatcher


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


# ---------- persistence / orphan handling / log / stderr ----------


async def test_finished_job_survives_dispatcher_reconstruction(
    fake_claude, state_path
):
    """A done job must still be retrievable after the bridge process
    'restarts'. We simulate a restart by constructing a second Dispatcher
    pointed at the same state path."""
    d1 = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    job_id = await d1.dispatch_async("hi", channel="alpha")
    final = await d1.wait_dispatch(job_id, max_wait_seconds=5)
    assert final["status"] == "done"

    # Tear down d1 (drop the live Task reference) and rebuild from disk.
    del d1
    d2 = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))

    reread = d2.get_dispatch(job_id)
    assert reread["status"] == "done"
    assert reread["ok"] is True
    assert reread["result"] == "hi"
    assert reread["job_id"] == job_id


async def test_inflight_job_marked_orphaned_after_restart(
    fake_claude, state_path, monkeypatch
):
    """If the bridge dies mid-dispatch, the next bridge must surface a
    definite terminal state, not "unknown job_id"."""
    monkeypatch.setenv("CLAUDE_FAKE_SLEEP", "5")
    d1 = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    job_id = await d1.dispatch_async("hi", channel="alpha", timeout_seconds=30)
    # Give the persistence write a moment to land before we 'crash'.
    await asyncio.sleep(0.05)
    assert d1.get_dispatch(job_id)["status"] == "running"

    # Cancel the live task so we don't leak a fake-claude subprocess into
    # the next test, then drop the dispatcher.
    d1.cancel_dispatch(job_id)
    await asyncio.sleep(0.05)
    del d1

    d2 = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    reread = d2.get_dispatch(job_id)
    # The persisted record was either "cancelled" (cancel landed before
    # restart) or "orphaned" (cancel didn't make it to disk in time).
    # Both are acceptable terminal states; the contract is that the job
    # is no longer "running" and the caller gets a definite answer.
    assert reread["status"] in {"orphaned", "cancelled"}
    assert reread["ok"] is False


async def test_orphaned_job_resets_its_channel_pinning(
    fake_claude, state_path
):
    """An orphaned in-flight job's channel must be auto-unpinned so a
    fresh dispatch on that channel doesn't race with the orphan
    subprocess by --resume'ing the same session id.

    Simulating a real crash from inside the asyncio loop is awkward —
    asyncio's shutdown machinery cancels everything cleanly, which is
    the *opposite* of the case we care about. So we seed the persisted
    files directly and verify the dispatcher does the right thing on
    construction.
    """
    state_path.parent.mkdir(parents=True, exist_ok=True)
    # Channel pinned to a session, as it would be after a successful prior
    # dispatch.
    state_path.write_text(
        json.dumps({"channels": {"alpha": "session-from-prior-run"}}),
        encoding="utf-8",
    )
    # And a "running" job from that crashed bridge.
    (state_path.parent / "jobs.json").write_text(
        json.dumps({
            "jobs": [{
                "id": "ghost-job",
                "channel": "alpha",
                "started_at": time.time() - 30,
                "args": {"channel": "alpha", "permission_mode": "acceptEdits"},
                "status": "running",
                "finished_at": None,
                "result": None,
                "error": None,
            }],
        }),
        encoding="utf-8",
    )

    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))

    # The orphaned job's channel pinning must be cleared.
    assert "alpha" not in d.list_channels(), d.list_channels()

    # The job itself is now a terminal "orphaned" record.
    reread = d.get_dispatch("ghost-job")
    assert reread["status"] == "orphaned"
    assert reread["ok"] is False
    assert "result was not captured" in reread["error"]

    # And the orphan-marking is durable: a second restart sees the
    # same final state, doesn't re-orphan, doesn't re-reset.
    del d
    d2 = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    assert d2.get_dispatch("ghost-job")["status"] == "orphaned"
    assert "alpha" not in d2.list_channels()


async def test_stderr_captured_on_success(fake_claude, state_path, monkeypatch):
    """Project MCP server warnings often arrive on stderr while claude
    still exits 0. Callers must be able to see them."""
    monkeypatch.setenv("CLAUDE_FAKE_STDERR", "playwright-persona disconnected")
    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    res = await d.dispatch("hi", channel="alpha")
    assert res.ok is True
    assert res.stderr == "playwright-persona disconnected"
    payload = res.to_dict()
    assert payload.get("stderr") == "playwright-persona disconnected"


async def test_log_writes_jsonl_events(
    fake_claude, state_path, tmp_path
):
    """Every state transition writes one JSONL line with at least an
    event type, timestamp, and job_id."""
    log_path = tmp_path / "bridge.log"
    d = Dispatcher(
        state_path=state_path,
        claude_bin=str(fake_claude),
        log_path=log_path,
    )
    job_id = await d.dispatch_async("hi", channel="alpha")
    final = await d.wait_dispatch(job_id, max_wait_seconds=5)
    assert final["status"] == "done"

    lines = [
        json.loads(line) for line in log_path.read_text().splitlines() if line.strip()
    ]
    events = [entry["event"] for entry in lines]
    assert "dispatch_start" in events
    assert "dispatch_end" in events
    end_entry = [e for e in lines if e["event"] == "dispatch_end"][0]
    assert end_entry["job_id"] == job_id
    assert end_entry["channel"] == "alpha"
    assert end_entry["ok"] is True


async def test_log_redacts_prompt_by_default(
    fake_claude, state_path, tmp_path
):
    log_path = tmp_path / "bridge.log"
    d = Dispatcher(
        state_path=state_path,
        claude_bin=str(fake_claude),
        log_path=log_path,
    )
    secret = "do not leak this prompt to the log"
    job_id = await d.dispatch_async(secret, channel="alpha")
    await d.wait_dispatch(job_id, max_wait_seconds=5)

    contents = log_path.read_text()
    assert secret not in contents, contents


async def test_log_prompts_opt_in_includes_prompt(
    fake_claude, state_path, tmp_path
):
    log_path = tmp_path / "bridge.log"
    d = Dispatcher(
        state_path=state_path,
        claude_bin=str(fake_claude),
        log_path=log_path,
        log_prompts=True,
    )
    secret = "this prompt SHOULD appear in the log"
    job_id = await d.dispatch_async(secret, channel="alpha")
    await d.wait_dispatch(job_id, max_wait_seconds=5)

    contents = log_path.read_text()
    assert secret in contents


async def test_persist_prompts_opt_in_writes_to_jobs_file(
    fake_claude, state_path
):
    """With ``persist_prompts=True``, the prompt is durably stored on
    disk so a post-mortem can see what was dispatched. Off by default."""
    d = Dispatcher(
        state_path=state_path,
        claude_bin=str(fake_claude),
        persist_prompts=True,
    )
    secret = "post-mortem prompt"
    job_id = await d.dispatch_async(secret, channel="alpha")
    await d.wait_dispatch(job_id, max_wait_seconds=5)

    jobs_file = state_path.parent / "jobs.json"
    persisted = json.loads(jobs_file.read_text())
    [entry] = [j for j in persisted["jobs"] if j["id"] == job_id]
    assert entry["args"]["prompt"] == secret


async def test_persist_prompts_off_by_default(fake_claude, state_path):
    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    secret = "this prompt should not be on disk"
    job_id = await d.dispatch_async(secret, channel="alpha")
    await d.wait_dispatch(job_id, max_wait_seconds=5)

    jobs_file = state_path.parent / "jobs.json"
    [entry] = [j for j in json.loads(jobs_file.read_text())["jobs"] if j["id"] == job_id]
    assert "prompt" not in entry["args"]
    # And the prompt isn't anywhere else in the file either.
    assert secret not in jobs_file.read_text()


async def test_corrupt_jobs_file_doesnt_crash_init(state_path, fake_claude):
    """A garbled jobs.json must be tolerated so the bridge can keep
    running on the channels we still know about."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    (state_path.parent / "jobs.json").write_text("{not valid json")
    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    assert d.list_jobs() == []
    # And a new dispatch still works.
    job_id = await d.dispatch_async("hi", channel="alpha")
    final = await d.wait_dispatch(job_id, max_wait_seconds=5)
    assert final["status"] == "done"


async def test_async_dispatch_writes_subprocess_output_to_files(
    fake_claude, state_path
):
    """The async path must redirect stdout/stderr to files under
    ``job-output/<job_id>``, so output survives even if our asyncio task
    is cancelled before reading the pipe."""
    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    job_id = await d.dispatch_async("hi", channel="alpha")
    final = await d.wait_dispatch(job_id, max_wait_seconds=5)
    assert final["status"] == "done"

    out_dir = state_path.parent / "job-output" / job_id
    assert out_dir.is_dir()
    stdout_text = (out_dir / "stdout").read_text()
    # Fake-claude wrote a JSON line to stdout, which is what dispatch parsed.
    assert "session_id" in stdout_text
    assert (out_dir / "stderr").exists()


async def test_subprocess_pid_persisted_to_jobs_file(
    fake_claude, state_path, monkeypatch
):
    """During a dispatch, the PID is on disk *before* we await
    completion. A bridge that crashes mid-dispatch can find the
    subprocess by reading jobs.json."""
    monkeypatch.setenv("CLAUDE_FAKE_SLEEP", "0.5")
    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    job_id = await d.dispatch_async("hi", channel="alpha")

    # PID and output_dir are persisted before claude exits.
    await asyncio.sleep(0.1)
    persisted = json.loads((state_path.parent / "jobs.json").read_text())
    [entry] = [j for j in persisted["jobs"] if j["id"] == job_id]
    assert entry["pid"] is not None and entry["pid"] > 0
    assert entry["output_dir"]
    assert Path(entry["output_dir"]).exists()

    await d.wait_dispatch(job_id, max_wait_seconds=5)


async def test_runtime_cancel_marks_abandoned_not_cancelled(
    fake_claude, state_path, monkeypatch
):
    """If the asyncio task is cancelled by the runtime (not via
    cancel_dispatch), the job becomes ``abandoned`` — leaving the
    subprocess alive so a watcher can finalize the result."""
    monkeypatch.setenv("CLAUDE_FAKE_SLEEP", "0.4")
    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))

    job_id = await d.dispatch_async("hi", channel="alpha", timeout_seconds=10)
    # Give it a beat to actually spawn the subprocess.
    await asyncio.sleep(0.05)
    job = d._jobs[job_id]
    assert job.task is not None
    # Cancel the task WITHOUT going through cancel_dispatch — simulates
    # a runtime cancellation (FastMCP transport disconnect, loop teardown).
    job.task.cancel()
    # Let the cancellation handler write to disk.
    with __import__("contextlib").suppress(BaseException):
        await job.task

    state = d.get_dispatch(job_id)
    assert state["status"] == "abandoned", state
    assert "transport disconnect" in state["error"] or "loop shutdown" in state["error"]
    # The subprocess should still be running for a moment; the watcher
    # will finalize it.
    final = await asyncio.wait_for(
        _wait_for_status(d, job_id, target_statuses={"done", "error"}),
        timeout=3,
    )
    assert final["status"] in {"done", "error"}


async def test_user_cancel_still_marks_cancelled(
    fake_claude, state_path, monkeypatch
):
    """``cancel_dispatch`` keeps its old user-visible semantics:
    status becomes ``cancelled``, not ``abandoned``."""
    monkeypatch.setenv("CLAUDE_FAKE_SLEEP", "5")
    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))

    job_id = await d.dispatch_async("hi", channel="alpha", timeout_seconds=30)
    await asyncio.sleep(0.05)
    cancel_res = d.cancel_dispatch(job_id)
    assert cancel_res["cancelled"] is True

    final = await d.wait_dispatch(job_id, max_wait_seconds=3)
    assert final["status"] == "cancelled"


async def _wait_for_status(d, job_id, target_statuses, poll=0.1, deadline=5.0):
    """Tiny helper — polls get_dispatch until status enters the target set."""
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        state = d.get_dispatch(job_id)
        if state["status"] in target_statuses:
            return state
        await asyncio.sleep(poll)
    return d.get_dispatch(job_id)


async def test_recovery_finalizes_completed_orphan(
    fake_claude, state_path
):
    """Most important durability test: bridge dies after subprocess
    finished. New bridge constructs, sees the persisted "running" job
    with output files on disk, and finalizes from those files."""
    state_path.parent.mkdir(parents=True, exist_ok=True)

    # Hand-craft an output dir with a successful claude JSON payload.
    job_id = "ghost-finished"
    out_dir = state_path.parent / "job-output" / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "stdout").write_text(
        json.dumps({
            "result": "recovered after restart",
            "session_id": "session-from-orphan",
            "is_error": False,
        })
    )
    (out_dir / "stderr").write_text("")

    # Persist a "running" job pointing at a PID that doesn't exist (so
    # _is_pid_alive returns False, triggering the dead-but-output-present
    # finalization path).
    (state_path.parent / "jobs.json").write_text(
        json.dumps({
            "jobs": [{
                "id": job_id,
                "channel": "alpha",
                "started_at": time.time() - 30,
                "args": {"channel": "alpha"},
                "status": "running",
                "finished_at": None,
                "result": None,
                "error": None,
                "pid": 999_999_999,  # not a real pid
                "output_dir": str(out_dir),
            }],
        })
    )

    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    state = d.get_dispatch(job_id)
    assert state["status"] == "done", state
    assert state["ok"] is True
    assert state["result"] == "recovered after restart"
    assert state["session_id"] == "session-from-orphan"


async def test_recovery_keeps_alive_orphan_running_until_watcher_finalizes(
    fake_claude, state_path
):
    """If a persisted job's PID is still alive at restart, the bridge
    leaves it as ``running`` and (after ``ensure_watchers_running``)
    the watcher reaps it when it exits."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    # We'll spawn a real long-running fake-claude as our 'orphan' so its
    # PID is alive. Run it ourselves via subprocess.Popen so it survives
    # this test's setup.
    import subprocess
    job_id = "ghost-alive"
    out_dir = state_path.parent / "job-output" / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = out_dir / "stdout"
    stderr_path = out_dir / "stderr"

    env = {**__import__("os").environ, "CLAUDE_FAKE_SLEEP": "1"}
    with stdout_path.open("wb") as so, stderr_path.open("wb") as se:
        proc = subprocess.Popen(
            [str(fake_claude), "-p", "hi"],
            stdin=subprocess.DEVNULL,
            stdout=so,
            stderr=se,
            env=env,
            start_new_session=True,
        )

    try:
        (state_path.parent / "jobs.json").write_text(
            json.dumps({
                "jobs": [{
                    "id": job_id,
                    "channel": "alpha",
                    "started_at": time.time(),
                    "args": {"channel": "alpha"},
                    "status": "running",
                    "finished_at": None,
                    "result": None,
                    "error": None,
                    "pid": proc.pid,
                    "output_dir": str(out_dir),
                }],
            })
        )

        d = Dispatcher(
            state_path=state_path,
            claude_bin=str(fake_claude),
            _watcher_poll_seconds=0.1,
        )
        # Subprocess is alive; status is still "running" right after init.
        assert d.get_dispatch(job_id)["status"] == "running"

        # Bootstrap the watcher and let it reap the subprocess.
        await d.ensure_watchers_running()
        final = await asyncio.wait_for(
            _wait_for_status(d, job_id, target_statuses={"done", "error"}),
            timeout=5,
        )
        assert final["status"] == "done", final
        assert final["ok"] is True
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


async def test_recovery_marks_orphan_when_pid_and_output_missing(
    fake_claude, state_path
):
    """If a persisted job has neither a live PID nor recoverable output,
    we fall back to the tombstone behavior: orphaned + reset channel."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"channels": {"alpha": "session-from-prior-run"}})
    )
    (state_path.parent / "jobs.json").write_text(
        json.dumps({
            "jobs": [{
                "id": "tombstone",
                "channel": "alpha",
                "started_at": time.time() - 10,
                "args": {"channel": "alpha"},
                "status": "running",
                "finished_at": None,
                "result": None,
                "error": None,
                "pid": None,
                "output_dir": None,
            }],
        })
    )

    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    state = d.get_dispatch("tombstone")
    assert state["status"] == "orphaned"
    assert "alpha" not in d.list_channels()


# ---------- recurring dispatches (schedules) ----------


async def test_schedule_dispatch_fires_repeated_ticks(
    fake_claude, state_path
):
    d = Dispatcher(
        state_path=state_path,
        claude_bin=str(fake_claude),
        _scheduler_poll_seconds=0.05,
        _min_schedule_interval_seconds=0.0,
    )
    res = await d.create_schedule(
        prompt="hi",
        channel="watcher",
        interval_seconds=0.2,
    )
    assert res["ok"], res
    schedule_id = res["schedule_id"]

    await d.ensure_watchers_running()
    # ~0.7s should produce at least 3 ticks (first immediate, then ~0.2s apart).
    await asyncio.sleep(0.8)
    sched = d.get_schedule(schedule_id)["schedule"]
    assert sched["tick_count"] >= 3, sched

    await d.cancel_schedule(schedule_id)
    # No more ticks after cancellation.
    final_count = d.get_schedule(schedule_id)["schedule"]["tick_count"]
    await asyncio.sleep(0.5)
    sched = d.get_schedule(schedule_id)["schedule"]
    assert sched["status"] == "cancelled"
    assert sched["tick_count"] == final_count


async def test_schedule_respects_until_seconds(fake_claude, state_path):
    d = Dispatcher(
        state_path=state_path,
        claude_bin=str(fake_claude),
        _scheduler_poll_seconds=0.05,
        _min_schedule_interval_seconds=0.0,
    )
    res = await d.create_schedule(
        prompt="hi",
        channel="watcher",
        interval_seconds=0.2,
        until_seconds=0.4,
    )
    schedule_id = res["schedule_id"]
    await d.ensure_watchers_running()

    # Wait past the deadline. Loop should mark the schedule completed.
    await asyncio.sleep(0.9)
    sched = d.get_schedule(schedule_id)["schedule"]
    assert sched["status"] == "completed", sched


async def test_schedule_self_cancels_on_stop_sentinel(
    fake_claude, state_path, monkeypatch
):
    """A tick whose result text contains the stop sentinel cancels the
    schedule; no further ticks fire."""
    monkeypatch.setenv("CLAUDE_FAKE_RESULT", f"all done {STOP_SENTINEL}")
    d = Dispatcher(
        state_path=state_path,
        claude_bin=str(fake_claude),
        _scheduler_poll_seconds=0.05,
        _min_schedule_interval_seconds=0.0,
    )
    res = await d.create_schedule(
        prompt="hi", channel="watcher", interval_seconds=0.2
    )
    schedule_id = res["schedule_id"]
    await d.ensure_watchers_running()

    # Give the scheduler one tick + the loop a chance to see the result.
    await asyncio.sleep(0.6)
    sched = d.get_schedule(schedule_id)["schedule"]
    assert sched["status"] == "cancelled", sched
    assert sched["tick_count"] == 1, sched
    assert STOP_SENTINEL in (sched.get("error") or "")


async def test_schedule_skips_when_prior_tick_running(
    fake_claude, state_path, monkeypatch
):
    """If the prior tick is still running when the next interval fires,
    don't stack — wait for the next interval."""
    monkeypatch.setenv("CLAUDE_FAKE_SLEEP", "0.5")
    d = Dispatcher(
        state_path=state_path,
        claude_bin=str(fake_claude),
        _scheduler_poll_seconds=0.05,
        _min_schedule_interval_seconds=0.0,
    )
    res = await d.create_schedule(
        prompt="hi", channel="watcher", interval_seconds=0.1
    )
    schedule_id = res["schedule_id"]
    await d.ensure_watchers_running()

    # 0.5s window — interval is 0.1 but each tick takes 0.5, so we
    # should see at most 1 tick fire (skip the rest).
    await asyncio.sleep(0.4)
    sched = d.get_schedule(schedule_id)["schedule"]
    assert sched["tick_count"] == 1, sched
    await d.cancel_schedule(schedule_id)


async def test_schedule_persists_across_reconstruction(
    fake_claude, state_path
):
    d1 = Dispatcher(
        state_path=state_path,
        claude_bin=str(fake_claude),
        _min_schedule_interval_seconds=0.0,
    )
    res = await d1.create_schedule(
        prompt="hi", channel="watcher", interval_seconds=60
    )
    schedule_id = res["schedule_id"]
    del d1

    d2 = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    sched = d2.get_schedule(schedule_id)
    assert sched["ok"] is True
    assert sched["schedule"]["status"] == "active"
    assert sched["schedule"]["channel"] == "watcher"


async def test_schedule_does_not_burst_after_long_gap(
    fake_claude, state_path
):
    """Persisted last_tick_at far in the past must not trigger 60
    catch-up ticks. Only one fires on the first iteration."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    sid = "ghost-schedule"
    long_ago = time.time() - 3600  # an hour back
    (state_path.parent / "schedules.json").write_text(
        json.dumps({
            "schedules": [{
                "id": sid,
                "prompt": "hi",
                "channel": "watcher",
                "interval_seconds": 60,
                "until": None,
                "args": {},
                "created_at": long_ago,
                "last_tick_at": long_ago,
                "last_job_id": None,
                "tick_count": 5,
                "status": "active",
                "error": None,
            }],
        })
    )
    d = Dispatcher(
        state_path=state_path,
        claude_bin=str(fake_claude),
        _scheduler_poll_seconds=0.05,
        _min_schedule_interval_seconds=0.0,
    )
    await d.ensure_watchers_running()
    await asyncio.sleep(0.4)
    sched = d.get_schedule(sid)["schedule"]
    # Started at 5 ticks; should fire exactly one in this window.
    assert sched["tick_count"] == 6, sched
    await d.cancel_schedule(sid)


async def test_schedule_rejects_too_short_interval(fake_claude, state_path):
    d = Dispatcher(
        state_path=state_path,
        claude_bin=str(fake_claude),
        _min_schedule_interval_seconds=10.0,
    )
    res = await d.create_schedule(
        prompt="hi", channel="watcher", interval_seconds=1.0
    )
    assert res["ok"] is False
    assert "interval_seconds" in res["error"]


async def test_schedule_rejects_both_until_and_until_seconds(
    fake_claude, state_path
):
    d = Dispatcher(
        state_path=state_path,
        claude_bin=str(fake_claude),
        _min_schedule_interval_seconds=0.0,
    )
    res = await d.create_schedule(
        prompt="hi",
        channel="watcher",
        interval_seconds=1.0,
        until="2099-01-01T00:00:00Z",
        until_seconds=10,
    )
    assert res["ok"] is False
    assert "until" in res["error"]


async def test_schedule_parses_iso_until(fake_claude, state_path):
    d = Dispatcher(
        state_path=state_path,
        claude_bin=str(fake_claude),
        _min_schedule_interval_seconds=0.0,
    )
    res = await d.create_schedule(
        prompt="hi",
        channel="watcher",
        interval_seconds=1.0,
        until="2099-01-01T00:00:00Z",
    )
    assert res["ok"] is True
    sched = res["schedule"]
    assert sched["until"] is not None and sched["until"] > time.time()


async def test_cancel_unknown_schedule_returns_clean_error(
    fake_claude, state_path
):
    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    res = await d.cancel_schedule("nope")
    assert res["ok"] is False
    assert "unknown" in res["error"]


# ---------- completion polling ----------


async def test_list_completions_filters_by_since(
    fake_claude, state_path
):
    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    j1 = await d.dispatch_async("a", channel="alpha")
    await d.wait_dispatch(j1, max_wait_seconds=5)
    cursor = d.get_dispatch(j1)["finished_at"]

    j2 = await d.dispatch_async("b", channel="beta")
    await d.wait_dispatch(j2, max_wait_seconds=5)

    comps = d.list_completions(since=cursor)
    job_ids = [c["job_id"] for c in comps]
    assert j1 not in job_ids
    assert j2 in job_ids


async def test_wait_any_completion_returns_when_job_finishes(
    fake_claude, state_path, monkeypatch
):
    monkeypatch.setenv("CLAUDE_FAKE_SLEEP", "0.3")
    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    cursor = time.time()
    job_id = await d.dispatch_async("hi", channel="alpha")

    comps = await d.wait_any_completion(since=cursor, max_wait_seconds=3)
    assert any(c["job_id"] == job_id for c in comps)


async def test_wait_any_completion_returns_empty_on_timeout(
    fake_claude, state_path
):
    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    comps = await d.wait_any_completion(
        since=time.time() + 1000, max_wait_seconds=0.3
    )
    assert comps == []


# ---------- bridge-owned event log + cursor ----------


async def test_log_event_recorded_in_memory_buffer(
    fake_claude, state_path
):
    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    job_id = await d.dispatch_async("hi", channel="alpha")
    await d.wait_dispatch(job_id, max_wait_seconds=5)

    events = d.list_events()
    types = [e["event"] for e in events]
    assert "dispatch_start" in types
    assert "dispatch_end" in types
    end = next(e for e in events if e["event"] == "dispatch_end")
    assert end["job_id"] == job_id
    assert end["channel"] == "alpha"
    assert end["ok"] is True


async def test_list_events_filters_by_since(
    fake_claude, state_path
):
    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    job_id = await d.dispatch_async("first", channel="alpha")
    await d.wait_dispatch(job_id, max_wait_seconds=5)
    cursor = max(e["ts"] for e in d.list_events())

    job2 = await d.dispatch_async("second", channel="beta")
    await d.wait_dispatch(job2, max_wait_seconds=5)

    new_events = d.list_events(since=cursor)
    new_job_ids = {e.get("job_id") for e in new_events if "job_id" in e}
    assert job_id not in new_job_ids
    assert job2 in new_job_ids


async def test_list_events_filters_by_types(
    fake_claude, state_path
):
    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    job_id = await d.dispatch_async("hi", channel="alpha")
    await d.wait_dispatch(job_id, max_wait_seconds=5)

    starts_only = d.list_events(types=["dispatch_start"])
    assert all(e["event"] == "dispatch_start" for e in starts_only)
    assert len(starts_only) == 1


async def test_events_persist_across_reconstruction(
    fake_claude, state_path
):
    d1 = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    job_id = await d1.dispatch_async("hi", channel="alpha")
    await d1.wait_dispatch(job_id, max_wait_seconds=5)
    pre_count = len(d1.list_events())
    del d1

    d2 = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    post_events = d2.list_events()
    assert len(post_events) >= pre_count
    types = [e["event"] for e in post_events]
    assert "dispatch_end" in types


async def test_events_bounded_by_max_events(state_path, fake_claude):
    d = Dispatcher(
        state_path=state_path, claude_bin=str(fake_claude), max_events=5
    )
    for _ in range(20):
        d._log_event("synthetic")
    events = d.list_events()
    assert len(events) == 5


async def test_notable_only_filters_out_chatter(fake_claude, state_path):
    """notable_only=True drops dispatch_start, schedule_tick, schedule_created
    but keeps terminal transitions and failures."""
    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    job_id = await d.dispatch_async("hi", channel="alpha")
    await d.wait_dispatch(job_id, max_wait_seconds=5)

    all_events = d.list_events()
    notable = d.list_events(notable_only=True)

    all_types = {e["event"] for e in all_events}
    notable_types = {e["event"] for e in notable}

    # Sanity: dispatch_start was logged at all.
    assert "dispatch_start" in all_types
    # Filter excludes the chatter.
    assert "dispatch_start" not in notable_types
    # But keeps the terminal transition.
    assert "dispatch_end" in notable_types


async def test_notable_only_keeps_failure_events(fake_claude, state_path):
    """webhook_failed and dispatch_error are notable."""
    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    # Webhook failure path — unreachable URL.
    job_id = await d.dispatch_async(
        "hi",
        channel="alpha",
        notify_url="http://127.0.0.1:1/never-listening",
    )
    await d.wait_dispatch(job_id, max_wait_seconds=5)
    # Give the webhook a beat to fail.
    for _ in range(40):
        if d.list_events(types=["webhook_failed"]):
            break
        await asyncio.sleep(0.05)

    notable_types = {e["event"] for e in d.list_events(notable_only=True)}
    assert "webhook_failed" in notable_types
    assert "webhook_sent" not in notable_types  # success is not notable


async def test_notable_only_composes_with_since_and_types(
    fake_claude, state_path
):
    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    j1 = await d.dispatch_async("a", channel="alpha")
    await d.wait_dispatch(j1, max_wait_seconds=5)
    cursor = max(e["ts"] for e in d.list_events())
    j2 = await d.dispatch_async("b", channel="beta")
    await d.wait_dispatch(j2, max_wait_seconds=5)

    # since + notable_only — only j2's terminal events.
    res = d.list_events(since=cursor, notable_only=True)
    assert all(e["event"] in {"dispatch_end"} or
               e["event"] not in {"dispatch_start"} for e in res)
    job_ids = {e.get("job_id") for e in res if "job_id" in e}
    assert j1 not in job_ids
    assert j2 in job_ids

    # since + types + notable_only intersect (types wins for explicit lookup).
    explicit = d.list_events(
        since=cursor,
        types=["dispatch_start"],  # would be notable_only=False's territory
        notable_only=True,
    )
    # dispatch_start isn't in NOTABLE_EVENTS — intersection is empty.
    assert explicit == []


async def test_list_events_oldest_first(fake_claude, state_path):
    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    d._log_event("a")
    await asyncio.sleep(0.01)
    d._log_event("b")
    events = d.list_events()
    a_idx = next(i for i, e in enumerate(events) if e["event"] == "a")
    b_idx = next(i for i, e in enumerate(events) if e["event"] == "b")
    assert a_idx < b_idx


# ---------- webhook notifications ----------


async def test_dispatch_webhook_fired_on_done(fake_claude, state_path):
    """A finished job whose notify_url is set must POST a JSON payload
    with event=done."""
    received: list[dict] = []
    url = await _start_capture_server(received)

    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    job_id = await d.dispatch_async(
        prompt="hi",
        channel="alpha",
        notify_url=url,
    )
    await d.wait_dispatch(job_id, max_wait_seconds=5)

    # Webhook is fired in a background executor — give it a tick.
    for _ in range(50):
        if received:
            break
        await asyncio.sleep(0.05)

    assert received, "expected one webhook delivery"
    payload = received[0]
    assert payload["event"] == "done"
    assert payload["job_id"] == job_id
    assert payload["channel"] == "alpha"
    assert payload["ok"] is True


async def test_dispatch_webhook_respects_notify_on(fake_claude, state_path):
    """notify_on=['error'] must NOT fire on a successful done."""
    received: list[dict] = []
    url = await _start_capture_server(received)

    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    job_id = await d.dispatch_async(
        prompt="hi",
        channel="alpha",
        notify_url=url,
        notify_on=["error"],
    )
    await d.wait_dispatch(job_id, max_wait_seconds=5)
    await asyncio.sleep(0.3)
    assert received == []


async def test_dispatch_webhook_failure_is_swallowed(
    fake_claude, state_path
):
    """If the destination is unreachable, the dispatch succeeds and the
    failure is recorded in the event log — never propagated."""
    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    job_id = await d.dispatch_async(
        prompt="hi",
        channel="alpha",
        notify_url="http://127.0.0.1:1/never-listening",
    )
    final = await d.wait_dispatch(job_id, max_wait_seconds=5)
    assert final["status"] == "done"
    # Give the background webhook attempt a beat to fail.
    for _ in range(40):
        events = d.list_events(types=["webhook_failed"])
        if events:
            break
        await asyncio.sleep(0.05)
    assert d.list_events(types=["webhook_failed"]), (
        "expected a webhook_failed event in the log"
    )


async def test_schedule_webhook_fired_on_schedule_end(
    fake_claude, state_path, monkeypatch
):
    """When a schedule self-cancels via the stop sentinel, schedule_end
    fires (the default notify_on)."""
    monkeypatch.setenv("CLAUDE_FAKE_RESULT", f"done {STOP_SENTINEL}")
    received: list[dict] = []
    url = await _start_capture_server(received)

    d = Dispatcher(
        state_path=state_path,
        claude_bin=str(fake_claude),
        _scheduler_poll_seconds=0.05,
        _min_schedule_interval_seconds=0.0,
    )
    res = await d.create_schedule(
        prompt="hi",
        channel="watcher",
        interval_seconds=0.1,
        notify_url=url,
    )
    schedule_id = res["schedule_id"]
    await d.ensure_watchers_running()

    # Wait for the sentinel to land + schedule_end webhook.
    for _ in range(60):
        if any(p["event"] == "schedule_end" for p in received):
            break
        await asyncio.sleep(0.05)

    end_payload = next(
        (p for p in received if p["event"] == "schedule_end"), None
    )
    assert end_payload is not None, received
    assert end_payload["schedule_id"] == schedule_id
    assert STOP_SENTINEL in (end_payload["last_tick_result"] or "")
    assert end_payload["status"] == "cancelled"


async def test_schedule_webhook_tick_with_sentinel_event(
    fake_claude, state_path, monkeypatch
):
    """Opt into ``tick_with_sentinel`` and you receive both that AND
    schedule_end (since the sentinel always implies a terminal transition)."""
    monkeypatch.setenv("CLAUDE_FAKE_RESULT", f"done {STOP_SENTINEL}")
    received: list[dict] = []
    url = await _start_capture_server(received)

    d = Dispatcher(
        state_path=state_path,
        claude_bin=str(fake_claude),
        _scheduler_poll_seconds=0.05,
        _min_schedule_interval_seconds=0.0,
    )
    await d.create_schedule(
        prompt="hi",
        channel="watcher",
        interval_seconds=0.1,
        notify_url=url,
        notify_on=["tick_with_sentinel", "schedule_end"],
    )
    await d.ensure_watchers_running()

    for _ in range(60):
        events = {p["event"] for p in received}
        if {"tick_with_sentinel", "schedule_end"} <= events:
            break
        await asyncio.sleep(0.05)

    types = [p["event"] for p in received]
    assert "tick_with_sentinel" in types
    assert "schedule_end" in types


# ---------- schedule chaining ----------


async def test_chained_schedule_starts_in_waiting(
    fake_claude, state_path
):
    d = Dispatcher(
        state_path=state_path,
        claude_bin=str(fake_claude),
        _min_schedule_interval_seconds=0.0,
    )
    pred = await d.create_schedule(
        prompt="a", channel="alpha", interval_seconds=60
    )
    succ = await d.create_schedule(
        prompt="b",
        channel="beta",
        interval_seconds=60,
        after_schedule_id=pred["schedule_id"],
    )
    assert succ["ok"] is True
    sched = succ["schedule"]
    assert sched["status"] == "waiting"
    assert sched["after_schedule_id"] == pred["schedule_id"]


async def test_chained_schedule_activates_when_predecessor_cancels(
    fake_claude, state_path
):
    d = Dispatcher(
        state_path=state_path,
        claude_bin=str(fake_claude),
        _scheduler_poll_seconds=0.05,
        _min_schedule_interval_seconds=0.0,
    )
    pred = await d.create_schedule(
        prompt="a", channel="alpha", interval_seconds=60
    )
    succ = await d.create_schedule(
        prompt="b",
        channel="beta",
        interval_seconds=0.5,
        after_schedule_id=pred["schedule_id"],
    )
    succ_id = succ["schedule_id"]
    await d.ensure_watchers_running()

    # Predecessor still active → successor stays waiting.
    await asyncio.sleep(0.3)
    assert d.get_schedule(succ_id)["schedule"]["status"] == "waiting"

    await d.cancel_schedule(pred["schedule_id"])
    # Successor should activate within a poll cycle and start firing.
    for _ in range(40):
        sched = d.get_schedule(succ_id)["schedule"]
        if sched["status"] == "active":
            break
        await asyncio.sleep(0.05)
    sched = d.get_schedule(succ_id)["schedule"]
    assert sched["status"] == "active", sched
    await d.cancel_schedule(succ_id)


async def test_chained_schedule_activates_immediately_when_pred_already_terminal(
    fake_claude, state_path
):
    d = Dispatcher(
        state_path=state_path,
        claude_bin=str(fake_claude),
        _min_schedule_interval_seconds=0.0,
    )
    pred = await d.create_schedule(
        prompt="a", channel="alpha", interval_seconds=60
    )
    await d.cancel_schedule(pred["schedule_id"])

    succ = await d.create_schedule(
        prompt="b",
        channel="beta",
        interval_seconds=60,
        after_schedule_id=pred["schedule_id"],
    )
    assert succ["schedule"]["status"] == "active"


async def test_chained_schedule_unknown_predecessor_rejected(
    fake_claude, state_path
):
    d = Dispatcher(
        state_path=state_path,
        claude_bin=str(fake_claude),
        _min_schedule_interval_seconds=0.0,
    )
    res = await d.create_schedule(
        prompt="b",
        channel="beta",
        interval_seconds=60,
        after_schedule_id="not-real",
    )
    assert res["ok"] is False
    assert "unknown after_schedule_id" in res["error"]


async def test_chained_schedule_cycle_rejected(
    fake_claude, state_path
):
    """A → B, then B → A should be rejected at creation time."""
    d = Dispatcher(
        state_path=state_path,
        claude_bin=str(fake_claude),
        _min_schedule_interval_seconds=0.0,
    )
    a = await d.create_schedule(
        prompt="a", channel="alpha", interval_seconds=60
    )
    b = await d.create_schedule(
        prompt="b",
        channel="beta",
        interval_seconds=60,
        after_schedule_id=a["schedule_id"],
    )
    # Now try to point A.after = B → cycle through B→A→B.
    # We can't mutate via create, but we can attempt to create C that
    # depends on A while A's chain is healthy. Real cycle detection
    # only matters at creation; our scheduler can't introduce one.
    # Force a cycle by hand-editing the in-memory chain to simulate
    # a corrupt persisted file.
    d._schedules[a["schedule_id"]].after_schedule_id = b["schedule_id"]
    res = await d.create_schedule(
        prompt="c",
        channel="gamma",
        interval_seconds=60,
        after_schedule_id=b["schedule_id"],  # b → a → b
    )
    assert res["ok"] is False
    assert "cycle" in res["error"].lower()


# ---------- helpers ----------


async def _start_capture_server(received: list[dict]):
    """Spin up a tiny HTTP server that captures the first POST body
    and returns it as a base URL. Caller can pass it as notify_url."""
    import http.server
    import threading

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            try:
                received.append(json.loads(body))
            except json.JSONDecodeError:
                received.append({"_raw": body})
            self.send_response(204)
            self.end_headers()

        def log_message(self, format, *args):  # noqa: A002 — match base signature
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    return f"http://127.0.0.1:{port}/"


# ---------- discoverability / bridge_help ----------


def test_dispatcher_module_exports_stop_sentinel():
    """Agents can import this from claude_bridge.dispatcher to spell it
    correctly in prompts."""
    from claude_bridge.dispatcher import STOP_SENTINEL as imported
    assert imported == "[BRIDGE_STOP_SCHEDULE]"


async def test_list_jobs_strips_raw_for_size(fake_claude, state_path):
    """``list_jobs`` is called repeatedly for diagnostics; ``raw`` can
    be multi-MB on real claude runs, so it's elided here. Full payload
    is still available via ``get_dispatch``."""
    d = Dispatcher(state_path=state_path, claude_bin=str(fake_claude))
    job_id = await d.dispatch_async("hi", channel="alpha")
    await d.wait_dispatch(job_id, max_wait_seconds=5)

    [summary] = d.list_jobs()
    assert summary["status"] == "done"
    assert "raw" not in summary
    full = d.get_dispatch(job_id)
    assert "raw" in full


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
