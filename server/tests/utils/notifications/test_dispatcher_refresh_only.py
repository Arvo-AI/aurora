"""dispatcher.notify_investigation_completed(refresh_only=True): a follow-up
re-summary refreshes the Google Chat card in place and announces nothing."""

import sys
import types
from unittest.mock import MagicMock

import pytest

from utils.notifications import dispatcher, slack_notification_service as svc


@pytest.fixture
def wired(monkeypatch):
    calls = {"slack": MagicMock(return_value=True), "gchat": MagicMock(return_value=True), "email": MagicMock()}
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


def test_refresh_only_edits_the_google_chat_card_and_posts_nothing(wired, monkeypatch):
    monkeypatch.setattr(dispatcher, "_get_incident_data", lambda incident_id, user_id: _incident())
    dispatcher.notify_investigation_completed("u1", "i1", session_id="s1", refresh_only=True)
    wired["email"].assert_not_called()
    wired["slack"].assert_not_called()
    wired["gchat"].assert_called_once_with("u1", _incident(), allow_new_message=False)


def test_refresh_only_without_a_card_skips_google_chat(wired, monkeypatch):
    monkeypatch.setattr(dispatcher, "_get_incident_data", lambda incident_id, user_id: _incident(google_chat_message_name=None))
    dispatcher.notify_investigation_completed("u1", "i1", refresh_only=True)
    wired["gchat"].assert_not_called()
    wired["slack"].assert_not_called()
