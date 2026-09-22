"""Tests for the Phase 6 team-routing agent dispatch.

trigger_team_routing_agent decides whether to fire the background agent that
posts to team channels (via post_slack_message) and builds its teammate prompt.
"""

from unittest.mock import MagicMock, patch

import utils.notifications.slack_team_routing as tr
from tests.utils.notifications.slack_fakes import folded_incident, standalone_incident


def _patches(*, connected=True, allowed=True, index=""):
    """Context managers patching the module's external dependencies."""
    delay = MagicMock()
    return {
        "is_slack_connected": patch(
            "chat.backend.agent.tools.slack_tool.is_slack_connected",
            return_value=connected,
        ),
        "read_index": patch(
            "services.memory.incident_index.read_index", return_value=index
        ),
        "session": patch(
            "chat.background.task.create_background_chat_session",
            return_value="sess-1",
        ),
        "allowed": patch(
            "chat.background.task.is_background_chat_allowed", return_value=allowed
        ),
        "run": patch("chat.background.task.run_background_chat"),
        "delay": delay,
    }


def _run(incident, **opts):
    p = _patches(**opts)
    with p["is_slack_connected"], p["read_index"], p["session"], p["allowed"], \
            p["run"] as run_mock:
        result = tr.trigger_team_routing_agent("u1", incident)
        return result, run_mock


def test_dispatches_agent_in_agent_mode():
    result, run_mock = _run(standalone_incident())
    assert result is True
    run_mock.delay.assert_called_once()
    kwargs = run_mock.delay.call_args.kwargs
    assert kwargs["mode"] == "agent"
    assert kwargs["send_notifications"] is False
    # Source must NOT be "slack" (that would post the agent reply to a source channel).
    assert kwargs["trigger_metadata"]["source"] == "team_routing"
    # Not linked as the RCA session — no re-run of the incident lifecycle.
    assert kwargs["incident_id"] is None


def test_no_incident_id_skips():
    result, run_mock = _run(standalone_incident(incident_id=None))
    assert result is False
    run_mock.delay.assert_not_called()


def test_slack_not_connected_skips():
    result, run_mock = _run(standalone_incident(), connected=False)
    assert result is False
    run_mock.delay.assert_not_called()


def test_rate_limited_skips():
    result, run_mock = _run(standalone_incident(), allowed=False)
    assert result is False
    run_mock.delay.assert_not_called()


def test_recurrence_context_in_prompt():
    result, run_mock = _run(folded_incident())
    assert result is True
    prompt = run_mock.delay.call_args.kwargs["initial_message"]
    assert "RECURRENCE" in prompt
    assert "thread_ts" in prompt
    # Occurrence position surfaced so the agent can say "occurrence N".
    assert "occurrence 2 of 3" in prompt


def test_standalone_has_no_recurrence_block():
    result, run_mock = _run(standalone_incident())
    prompt = run_mock.delay.call_args.kwargs["initial_message"]
    assert "RECURRENCE" not in prompt


def test_incident_index_injected_when_present():
    result, run_mock = _run(standalone_incident(), index="- [INC 1 | d | api | resolved] disk full")
    prompt = run_mock.delay.call_args.kwargs["initial_message"]
    assert "INCIDENT_INDEX" in prompt
    assert "disk full" in prompt


def test_untrusted_fields_are_delimited():
    # Every externally-derived value must sit inside <<...>> data fences so
    # injected text can't be read as instructions.
    result, run_mock = _run(folded_incident(alert_title="High CPU", service="api"))
    prompt = run_mock.delay.call_args.kwargs["initial_message"]
    for fence in ("<<INCIDENT_TITLE>>", "<<SERVICE>>", "<<SEVERITY>>",
                  "<<CONCLUSION>>", "<<ANCHOR_TITLE>>"):
        assert fence in prompt


def test_rail_text_covers_all_interpolated_fields():
    result, run_mock = _run(
        folded_incident(alert_title="High CPU", aurora_summary="Root cause: disk full.",
                        service="payments", severity="sev1", anchor_alert_title="Orig CPU"),
    )
    rail = run_mock.delay.call_args.kwargs["rail_text"]
    # Every untrusted value interpolated into the prompt is present in rail_text.
    for field in ("High CPU", "disk full", "payments", "sev1", "Orig CPU"):
        assert field in rail
    # Recurrence counts (occurrence 2 of 3) are covered too.
    assert "2" in rail and "3" in rail


def test_rail_text_is_incident_fields_only():
    result, run_mock = _run(standalone_incident(alert_title="High CPU",
                                                aurora_summary="Root cause: disk full."))
    rail = run_mock.delay.call_args.kwargs["rail_text"]
    assert "High CPU" in rail
    assert "disk full" in rail
    # None of our instruction scaffolding leaks into the rail text.
    assert "get_connected_slack_channels" not in rail
