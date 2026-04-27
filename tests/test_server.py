"""Smoke tests for the MCP-facing layer.

The dispatcher is exercised in detail in ``test_dispatcher.py``. This
file covers the MCP shim:

* ``bridge_help`` returns a complete capability map keyed by tool name,
  with workflow patterns and the stop-sentinel constant. An agent that
  calls this once should be able to use the full surface correctly.
* The MCP tool list contains every advertised tool.
"""

from __future__ import annotations

from claude_bridge import server


def test_bridge_help_lists_every_advertised_tool():
    help_doc = server.bridge_help()
    described = set(help_doc["tools"].keys())
    expected = {
        "dispatch",
        "dispatch_async",
        "wait_dispatch",
        "get_dispatch",
        "cancel_dispatch",
        "list_jobs",
        "schedule_dispatch",
        "list_schedules",
        "get_schedule",
        "cancel_schedule",
        "list_completions",
        "wait_any_completion",
        "list_channels",
        "reset_channel",
    }
    missing = expected - described
    assert not missing, f"bridge_help missing tools: {missing}"


def test_bridge_help_includes_workflows_and_concepts():
    help_doc = server.bridge_help()
    workflow_names = {w["name"] for w in help_doc["workflows"]}
    # Each named pattern is one we want every agent to recognize.
    assert any("Short prompt" in n for n in workflow_names)
    assert any("Long prompt" in n for n in workflow_names)
    assert any("Watch" in n or "recurring" in n.lower() for n in workflow_names)
    assert any("completions" in n.lower() or "turn" in n.lower() for n in workflow_names)
    assert any("offline" in n.lower() or "event log" in n.lower() for n in workflow_names)
    assert any("Pipeline" in n or "after" in n.lower() for n in workflow_names)
    assert any("Push" in n or "webhook" in n.lower() for n in workflow_names)
    # Concepts cover the load-bearing ones.
    for k in (
        "channel",
        "session_pinning",
        "permission_mode",
        "cwd",
        "stop_sentinel",
        "schedule_chaining",
        "webhooks",
        "event_log",
    ):
        assert k in help_doc["concepts"], f"missing concept: {k}"


def test_bridge_help_advertises_stop_sentinel_literally():
    """Agents need the exact spelling to embed in prompts."""
    help_doc = server.bridge_help()
    assert help_doc["stop_sentinel"] == "[BRIDGE_STOP_SCHEDULE]"


def test_bridge_help_lists_notable_event_types():
    """An agent calling list_events(notable_only=True) should be able to
    learn from bridge_help which event types make the cut."""
    help_doc = server.bridge_help()
    notable = set(help_doc["notable_event_types"])
    # Spot-check the load-bearing entries on both sides of the line.
    assert "dispatch_end" in notable
    assert "schedule_self_cancelled" in notable
    assert "webhook_failed" in notable
    assert "dispatch_start" not in notable  # chatty
    assert "schedule_tick" not in notable   # chatty
    assert "webhook_sent" not in notable    # success delivery


def test_bridge_help_durability_section_is_specific():
    help_doc = server.bridge_help()
    persists = help_doc["durability"]["persists_to_disk"]
    # Should call out each persisted artifact concretely.
    joined = " ".join(persists).lower()
    for fragment in ("session", "job", "schedule", "output"):
        assert fragment in joined, f"durability missing mention of: {fragment}"


def test_bridge_help_gotchas_warn_about_known_traps():
    """The listed gotchas are the ones that have actually bitten us in
    practice. If anyone trims the list, this test forces a deliberate
    decision rather than silent loss."""
    help_doc = server.bridge_help()
    gotchas_text = " ".join(help_doc["gotchas"]).lower()
    for required in ("cwd", "abandoned", "bypasspermissions"):
        assert required in gotchas_text, f"gotcha missing: {required!r}"


def test_mcp_tools_list_matches_help_index():
    """Every name in bridge_help['tools'] should also appear in the MCP
    tool registry (so agents calling tools/list see them all)."""
    import asyncio
    help_doc = server.bridge_help()
    advertised = set(help_doc["tools"].keys())
    registered = {t.name for t in asyncio.run(server.mcp.list_tools())}
    # bridge_help itself is registered too; advertised excludes it
    # because it's the helper.
    assert advertised <= registered, advertised - registered
    # And the inverse: every registered tool (minus bridge_help) is
    # advertised in the help index.
    assert registered - {"bridge_help"} <= advertised, (
        registered - {"bridge_help"}
    ) - advertised
