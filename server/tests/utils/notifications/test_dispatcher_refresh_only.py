"""dispatcher.notify_investigation_completed(refresh_only=True): a follow-up
re-summary refreshes the Google Chat card in place and announces nothing
(no email, no Slack post, no PagerDuty note, no incident.io update)."""

import sys
import types
from unittest.mock import MagicMock

import pytest

from utils.notifications import dispatcher, slack_notification_service as svc


@pytest.fixture
def wired(monkeypatch):
    calls = {
        "slack": MagicMock(return_value=True), "gchat": MagicMock(return_value=True),
        "email": MagicMock(), "pd": MagicMock(return_value=True), "incidentio": MagicMock(return_value=True),
    }
    monkeypatch.setattr(dispatcher, "get_org_id_for_user", lambda user_id: "org-1")
    monkeypatch.setattr(dispatcher, "get_org_preference", lambda org_id, key, default=None: True)
    monkeypatch.setattr(dispatcher, "_has_slack_connected", lambda user_id: True)
    monkeypatch.setattr(dispatcher, "_has_teams_connected", lambda user_id: False)
    monkeypatch.setattr(dispatcher, "_has_google_chat_connected", lambda user_id: True)
    monkeypatch.setattr(dispatcher, "_send_emails", calls["email"])
    monkeypatch.setattr(dispatcher, "_enrich_incident_summary", lambda *a, **k: None)
    monkeypatch.setattr(svc, "send_slack_investigation_completed_notification", calls["slack"])
    for event in ("started", "failed"):
        calls[event] = MagicMock()
        monkeypatch.setattr(svc, f"send_slack_investigation_{event}_notification", calls[event])
    calls["reply"] = MagicMock()
    from tasks import slack_source_reply
    monkeypatch.setattr(slack_source_reply, "queue_source_reply", calls["reply"])
    gchat = types.ModuleType("utils.notifications.google_chat_notification_service")
    gchat.send_google_chat_investigation_completed_notification = calls["gchat"]
    monkeypatch.setitem(sys.modules, "utils.notifications.google_chat_notification_service", gchat)
    pd = types.ModuleType("utils.notifications.pagerduty_notification_service")
    pd.send_pagerduty_incident_note = calls["pd"]
    monkeypatch.setitem(sys.modules, "utils.notifications.pagerduty_notification_service", pd)
    incidentio = types.ModuleType("utils.notifications.incidentio_notification_service")
    incidentio.send_incidentio_incident_update = calls["incidentio"]
    monkeypatch.setitem(sys.modules, "utils.notifications.incidentio_notification_service", incidentio)
    monkeypatch.setattr(dispatcher, "get_user_preference", lambda user_id, key, default=None: True)
    return calls


def _incident(**over):
    data = {"incident_id": "i1", "alert_title": "High CPU", "google_chat_message_name": "spaces/s/messages/m"}
    data.update(over)
    return data


def test_first_completion_announces_on_enabled_channels(wired, monkeypatch):
    monkeypatch.setattr(dispatcher, "_get_incident_data", lambda incident_id, user_id: _incident())
    dispatcher.notify_investigation_completed("u1", "i1", session_id="s1")
    wired["email"].assert_called_once()
    wired["slack"].assert_not_called()
    wired["gchat"].assert_called_once_with("u1", _incident(), allow_new_message=True)
    wired["pd"].assert_called_once_with("u1", _incident())


def test_refresh_only_edits_the_google_chat_card_and_posts_nothing(wired, monkeypatch):
    monkeypatch.setattr(dispatcher, "_get_incident_data", lambda incident_id, user_id: _incident())
    dispatcher.notify_investigation_completed("u1", "i1", session_id="s1", refresh_only=True)
    wired["email"].assert_not_called()
    wired["slack"].assert_not_called()
    wired["gchat"].assert_called_once_with("u1", _incident(), allow_new_message=False)
    wired["pd"].assert_not_called()


def test_pagerduty_note_needs_the_org_opt_in(wired, monkeypatch):
    monkeypatch.setattr(dispatcher, "_get_incident_data", lambda incident_id, user_id: _incident())
    monkeypatch.setattr(
        dispatcher, "get_org_preference",
        lambda org_id, key, default=None: default if key == "pagerduty_incident_notes" else True,
    )
    dispatcher.notify_investigation_completed("u1", "i1", session_id="s1")
    wired["slack"].assert_not_called()
    wired["pd"].assert_not_called()


def test_incidentio_update_posts_on_first_completion_only(wired, monkeypatch):
    incident = _incident(source_type="incidentio")
    monkeypatch.setattr(dispatcher, "_get_incident_data", lambda incident_id, user_id: incident)
    dispatcher.notify_investigation_completed("u1", "i1", session_id="s1")
    wired["incidentio"].assert_called_once_with("u1", incident)
    wired["incidentio"].reset_mock()
    dispatcher.notify_investigation_completed("u1", "i1", session_id="s1", refresh_only=True)
    wired["incidentio"].assert_not_called()


def test_incidentio_update_needs_the_postback_toggle(wired, monkeypatch):
    monkeypatch.setattr(dispatcher, "_get_incident_data", lambda incident_id, user_id: _incident(source_type="incidentio"))
    monkeypatch.setattr(
        dispatcher, "get_user_preference",
        lambda user_id, key, default=None: default if key == "incidentio_postback_enabled" else True,
    )
    dispatcher.notify_investigation_completed("u1", "i1", session_id="s1")
    wired["slack"].assert_not_called()
    wired["incidentio"].assert_not_called()


def test_incidentio_update_never_runs_for_other_sources(wired, monkeypatch):
    monkeypatch.setattr(dispatcher, "_get_incident_data", lambda incident_id, user_id: _incident(source_type="pagerduty"))
    looked_up = []
    monkeypatch.setattr(dispatcher, "get_user_preference", lambda user_id, key, default=None: looked_up.append(key) or True)
    dispatcher.notify_investigation_completed("u1", "i1", session_id="s1")
    wired["incidentio"].assert_not_called()
    assert looked_up == []  # no preference round-trip for non-incident.io incidents


def test_refresh_only_without_a_card_skips_google_chat(wired, monkeypatch):
    monkeypatch.setattr(dispatcher, "_get_incident_data", lambda incident_id, user_id: _incident(google_chat_message_name=None))
    dispatcher.notify_investigation_completed("u1", "i1", refresh_only=True)
    wired["gchat"].assert_not_called()
    wired["slack"].assert_not_called()


@pytest.mark.parametrize("source", ["cloudwatch", "sentry", "pagerduty", "incidentio", "datadog"])
def test_investigations_have_no_slack_status_cards(source, routing, wired, monkeypatch):
    incident = _incident(source_type=source)
    monkeypatch.setattr(dispatcher, "_get_incident_data", lambda *args: incident)
    dispatcher.notify_investigation_started("u1", "i1")
    dispatcher.notify_investigation_failed("u1", "i1")
    wired["started"].assert_not_called()
    wired["failed"].assert_not_called()
    wired["reply"].assert_not_called()
    routing.assert_not_called()

    dispatcher.notify_investigation_completed("u1", "i1")
    wired["slack"].assert_not_called()
    if source in ("cloudwatch", "sentry", "pagerduty", "incidentio"):
        wired["reply"].assert_called_once_with("u1", "i1")
    else:
        wired["reply"].assert_not_called()
    routing.assert_called_once_with("u1", incident)


@pytest.mark.parametrize("partial,refresh", [(True, False), (False, True)])
def test_source_reply_and_router_wait_for_first_conclusion(partial, refresh, routing, wired, monkeypatch):
    incident = _incident(source_type="sentry", unfinished_reason="time_limit" if partial else None)
    monkeypatch.setattr(dispatcher, "_get_incident_data", lambda *args: incident)
    dispatcher.notify_investigation_completed("u1", "i1", refresh_only=refresh)
    wired["slack"].assert_not_called()
    wired["reply"].assert_not_called()
    routing.assert_not_called()


# --- team-channel routing: dispatched once per incident from the dispatcher ---------------

@pytest.fixture
def routing(wired, monkeypatch):
    from utils.notifications import team_routing

    mock = MagicMock(return_value=True)
    monkeypatch.setattr(team_routing, "trigger_team_routing_agent", mock)
    monkeypatch.setattr(dispatcher, "_has_teams_connected", lambda user_id: False)
    monkeypatch.setattr(dispatcher, "_get_incident_data", lambda incident_id, user_id: _incident())
    return mock


def test_routing_fires_once_per_completed_incident(routing):
    dispatcher.notify_investigation_completed("u1", "i1", session_id="s1")
    routing.assert_called_once_with("u1", _incident())


def test_routing_is_skipped_on_refresh_only(routing):
    dispatcher.notify_investigation_completed("u1", "i1", session_id="s1", refresh_only=True)
    routing.assert_not_called()


def test_routing_still_runs_when_thread_replies_are_off(routing, wired, monkeypatch):
    monkeypatch.setattr(dispatcher, "_get_incident_data", lambda *args: _incident(source_type="cloudwatch"))
    monkeypatch.setattr(
        dispatcher, "get_org_preference",
        lambda org_id, key, default=None: False if key == "slack_investigation_complete_notifications" else True,
    )
    dispatcher.notify_investigation_completed("u1", "i1", session_id="s1")
    wired["reply"].assert_not_called()
    routing.assert_called_once()


@pytest.mark.parametrize(("source", "postback"), [("pagerduty", "pd"), ("incidentio", "incidentio")])
def test_connector_reply_opt_out_preserves_native_postback_and_routing(source, postback, routing, wired, monkeypatch):
    incident = _incident(source_type=source)
    monkeypatch.setattr(dispatcher, "_get_incident_data", lambda *args: incident)
    monkeypatch.setattr(dispatcher, "get_org_preference",
                        lambda org, key, default=None: key != f"slack_{source}_rca_replies")

    dispatcher.notify_investigation_completed("u1", "i1")
    wired["reply"].assert_not_called()
    wired[postback].assert_called_once_with("u1", incident)
    routing.assert_called_once_with("u1", incident)

    incident["source_type"] = "sentry"
    dispatcher.notify_investigation_completed("u1", "i2")
    wired["reply"].assert_called_once_with("u1", "i2")


def test_routing_runs_for_a_teams_only_org(routing, monkeypatch):
    monkeypatch.setattr(dispatcher, "_has_slack_connected", lambda user_id: False)
    monkeypatch.setattr(dispatcher, "_has_teams_connected", lambda user_id: True)
    teams_svc = types.ModuleType("utils.notifications.teams_notification_service")
    teams_svc.send_teams_investigation_completed_notification = MagicMock(return_value=True)
    monkeypatch.setitem(sys.modules, "utils.notifications.teams_notification_service", teams_svc)
    dispatcher.notify_investigation_completed("u1", "i1", session_id="s1")
    routing.assert_called_once()


def test_routing_is_skipped_with_no_chat_platform_connected(routing, monkeypatch):
    monkeypatch.setattr(dispatcher, "_has_slack_connected", lambda user_id: False)
    dispatcher.notify_investigation_completed("u1", "i1", session_id="s1")
    routing.assert_not_called()


def test_a_routing_failure_does_not_break_the_other_notifications(routing, wired):
    routing.side_effect = RuntimeError("agent queue down")
    dispatcher.notify_investigation_completed("u1", "i1", session_id="s1")
    wired["slack"].assert_not_called()
    wired["gchat"].assert_called_once()
