"""dispatcher.notify_investigation_completed(refresh_only=True): a follow-up
re-summary refreshes the Google Chat card in place and announces nothing
(no email, no Slack post, no PagerDuty note)."""

import sys
import types
from unittest.mock import MagicMock

import pytest

from utils.notifications import dispatcher, slack_notification_service as svc


@pytest.fixture
def wired(monkeypatch):
    calls = {
        "slack": MagicMock(return_value=True), "gchat": MagicMock(return_value=True),
        "email": MagicMock(), "pd": MagicMock(return_value=True),
    }
    monkeypatch.setattr(dispatcher, "get_org_id_for_user", lambda user_id: "org-1")
    monkeypatch.setattr(dispatcher, "get_org_preference", lambda org_id, key, default=None: True)
    monkeypatch.setattr(dispatcher, "_has_slack_connected", lambda user_id: True)
    monkeypatch.setattr(dispatcher, "_has_google_chat_connected", lambda user_id: True)
    monkeypatch.setattr(dispatcher, "_send_emails", calls["email"])
    monkeypatch.setattr(dispatcher, "_enrich_incident_summary", lambda *a, **k: None)
    monkeypatch.setattr(svc, "send_slack_investigation_completed_notification", calls["slack"])
    gchat = types.ModuleType("utils.notifications.google_chat_notification_service")
    gchat.send_google_chat_investigation_completed_notification = calls["gchat"]
    monkeypatch.setitem(sys.modules, "utils.notifications.google_chat_notification_service", gchat)
    pd = types.ModuleType("utils.notifications.pagerduty_notification_service")
    pd.send_pagerduty_incident_note = calls["pd"]
    monkeypatch.setitem(sys.modules, "utils.notifications.pagerduty_notification_service", pd)
    return calls


def _incident(**over):
    data = {"incident_id": "i1", "alert_title": "High CPU", "google_chat_message_name": "spaces/s/messages/m"}
    data.update(over)
    return data


def test_first_completion_announces_everywhere(wired, monkeypatch):
    monkeypatch.setattr(dispatcher, "_get_incident_data", lambda incident_id, user_id: _incident())
    dispatcher.notify_investigation_completed("u1", "i1", session_id="s1")
    wired["email"].assert_called_once()
    wired["slack"].assert_called_once()
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
    wired["slack"].assert_called_once()
    wired["pd"].assert_not_called()


def test_refresh_only_without_a_card_skips_google_chat(wired, monkeypatch):
    monkeypatch.setattr(dispatcher, "_get_incident_data", lambda incident_id, user_id: _incident(google_chat_message_name=None))
    dispatcher.notify_investigation_completed("u1", "i1", refresh_only=True)
    wired["gchat"].assert_not_called()
    wired["slack"].assert_not_called()


def test_card_toggle_off_still_runs_slack_for_routing(wired, monkeypatch):
    """The "Investigation Complete" toggle governs only the incidents-channel
    card. With it off, the dispatcher must STILL invoke the Slack notifier so
    description-driven team-channel routing runs — just with post_primary_card
    False."""
    monkeypatch.setattr(dispatcher, "_get_incident_data", lambda incident_id, user_id: _incident())
    # Card toggle off; everything else (slack connected) unchanged.
    monkeypatch.setattr(
        dispatcher, "get_org_preference",
        lambda org_id, key, default=None: False if key == "slack_investigation_complete_notifications" else True,
    )
    dispatcher.notify_investigation_completed("u1", "i1", session_id="s1")
    wired["slack"].assert_called_once_with("u1", _incident(), post_primary_card=False)


def test_card_toggle_on_passes_primary_card_true(wired, monkeypatch):
    monkeypatch.setattr(dispatcher, "_get_incident_data", lambda incident_id, user_id: _incident())
    dispatcher.notify_investigation_completed("u1", "i1", session_id="s1")
    wired["slack"].assert_called_once_with("u1", _incident(), post_primary_card=True)
