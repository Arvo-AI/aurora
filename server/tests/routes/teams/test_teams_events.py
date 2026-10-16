"""POST /teams/messages: auth, serviceUrl trust, dedupe, DMs and id normalisation."""

import pytest
from flask import Flask

from routes.teams import teams_events as ev
from routes.teams import teams_installs

SERVICE_URL = "https://smba.trafficmanager.net/teams/"
BOT_ID = "28:app-id"


@pytest.fixture
def app():
    flask_app = Flask(__name__)
    flask_app.register_blueprint(ev.teams_events_bp, url_prefix="/teams")
    return flask_app


@pytest.fixture
def wired(monkeypatch):
    """Authenticated by default; records every dispatch to Aurora."""
    state = {"dispatched": [], "claims": {"serviceurl": SERVICE_URL}, "seen": set(), "user": "uid-admin"}

    import utils.auth.stateless_auth as sa

    monkeypatch.setenv("TEAMS_APP_ID", "app-id")
    # The handler stamps "last bot message" on the org; no DB in these tests
    monkeypatch.setattr(sa, "get_org_id_for_user", lambda user_id: None)
    monkeypatch.setattr(ev, "verify_teams_request_claims", lambda: state["claims"])
    monkeypatch.setattr(ev, "get_user_id_from_teams_tenant", lambda tenant: state["user"])
    monkeypatch.setattr(ev, "send_message_to_aurora", lambda *a, **k: state["dispatched"].append((a, k)))
    state["installed"] = []
    state["urls"] = []
    state["updates"] = []
    monkeypatch.setattr(teams_installs, "mark_team_installed", lambda uid, team: state["installed"].append(team))
    monkeypatch.setattr(teams_installs, "remember_service_url", lambda uid, url: state["urls"].append(url))
    monkeypatch.setattr(ev, "handle_conversation_update", lambda uid, act: state["updates"].append((uid, act)))

    def claim_once(activity_id):
        if activity_id in state["seen"]:
            return False
        state["seen"].add(activity_id)
        return True

    monkeypatch.setattr(ev, "claim_activity_once", claim_once)
    return state


def _channel_activity(**over):
    activity = {
        "type": "message",
        "id": "act-1",
        "text": "<at>Aurora</at> why is checkout slow?",
        "serviceUrl": SERVICE_URL,
        "recipient": {"id": BOT_ID},
        "entities": [{"type": "mention", "mentioned": {"id": BOT_ID}}],
        "conversation": {
            "id": "19:chan@thread.tacv2;messageid=1700000000000",
            "conversationType": "channel",
            "tenantId": "tenant-1",
        },
        "channelData": {
            "team": {"id": "19:team@thread.tacv2", "aadGroupId": "11111111-2222-3333-4444-555555555555"},
            "channel": {"id": "19:chan@thread.tacv2"},
        },
    }
    activity.update(over)
    return activity


def _dm_activity(**over):
    activity = {
        "type": "message",
        "id": "dm-1",
        "text": "what's broken right now?",
        "serviceUrl": SERVICE_URL,
        "recipient": {"id": BOT_ID},
        "conversation": {"id": "a:1dm-conversation", "conversationType": "personal", "tenantId": "tenant-1"},
        "channelData": {"tenant": {"id": "tenant-1"}},
    }
    activity.update(over)
    return activity


def _post(app, activity):
    return app.test_client().post("/teams/messages", json=activity)


# --- auth -----------------------------------------------------------------------

def test_unverified_request_is_rejected(app, wired):
    wired["claims"] = None
    assert _post(app, _channel_activity()).status_code == 403
    assert wired["dispatched"] == []


def test_other_activity_types_are_ignored(app, wired):
    resp = _post(app, _channel_activity(type="typing"))
    assert resp.status_code == 200
    assert wired["dispatched"] == [] and wired["updates"] == []


# --- conversationUpdate ------------------------------------------------------------------

def _update_activity(**over):
    activity = _channel_activity(type="conversationUpdate", id="upd-1", text=None, entities=[])
    activity.update(over)
    return activity


def test_conversation_update_is_applied_without_dispatching_a_reply(app, wired):
    assert _post(app, _update_activity()).status_code == 200
    (uid, _activity), = wired["updates"]
    assert uid == "uid-admin"
    assert wired["dispatched"] == []
    # The regional Bot Connector host is remembered for later probes
    assert wired["urls"] == [SERVICE_URL]


def test_conversation_update_from_unknown_tenant_is_ignored(app, wired):
    wired["user"] = None
    assert _post(app, _update_activity()).status_code == 200
    assert wired["updates"] == []


def test_conversation_update_with_untrusted_service_url_is_rejected(app, wired):
    resp = _post(app, _update_activity(serviceUrl="https://evil.example.com/"))
    assert resp.status_code == 403
    assert wired["updates"] == []


def test_retried_conversation_update_is_applied_once(app, wired):
    _post(app, _update_activity())
    _post(app, _update_activity())
    assert len(wired["updates"]) == 1


def test_a_failing_update_never_fails_the_webhook(app, wired, monkeypatch):
    def boom(uid, act):
        raise RuntimeError("db down")

    monkeypatch.setattr(ev, "handle_conversation_update", boom)
    # Bot Framework would retry a 500 forever
    assert _post(app, _update_activity()).status_code == 200


def test_channel_mention_confirms_the_team_is_installed(app, wired):
    _post(app, _channel_activity())
    assert wired["installed"] == ["11111111-2222-3333-4444-555555555555"]


def test_dm_has_no_team_to_confirm(app, wired):
    _post(app, _dm_activity())
    assert wired["installed"] == []


# --- serviceUrl trust ---------------------------------------------------------------

def test_untrusted_service_url_is_rejected(app, wired):
    resp = _post(app, _channel_activity(serviceUrl="https://evil.example.com/"))
    assert resp.status_code == 403
    assert wired["dispatched"] == []


def test_service_url_not_matching_the_token_claim_is_rejected(app, wired):
    wired["claims"] = {"serviceurl": "https://smba.trafficmanager.net/emea/"}
    resp = _post(app, _channel_activity())
    assert resp.status_code == 403
    assert wired["dispatched"] == []


# --- dedupe ---------------------------------------------------------------------------

def test_retried_activity_is_processed_once(app, wired):
    assert _post(app, _channel_activity()).status_code == 200
    assert _post(app, _channel_activity()).status_code == 200
    assert len(wired["dispatched"]) == 1


# --- channel messages ---------------------------------------------------------------

def test_channel_message_without_mention_is_ignored(app, wired):
    resp = _post(app, _channel_activity(entities=[]))
    assert resp.status_code == 200
    assert wired["dispatched"] == []


def test_channel_mention_dispatches_with_graph_usable_ids(app, wired):
    _post(app, _channel_activity())
    (args, kwargs), = wired["dispatched"]
    assert args == ("uid-admin", "why is checkout slow?")
    # Graph team GUID and a channel id without the ;messageid= suffix
    assert kwargs["team_id"] == "11111111-2222-3333-4444-555555555555"
    assert kwargs["channel_id"] == "19:chan@thread.tacv2"
    # The reply still goes into the thread's own conversation
    assert kwargs["conversation_id"] == "19:chan@thread.tacv2;messageid=1700000000000"
    assert kwargs["service_url"] == SERVICE_URL
    assert kwargs["reply_to_id"] == "act-1"


def test_group_chat_needs_a_mention_too(app, wired):
    activity = _channel_activity(entities=[])
    activity["conversation"]["conversationType"] = "groupChat"
    _post(app, activity)
    assert wired["dispatched"] == []


def test_unknown_tenant_is_not_dispatched(app, wired):
    wired["user"] = None
    assert _post(app, _channel_activity()).status_code == 200
    assert wired["dispatched"] == []


def test_channel_message_without_team_is_ignored(app, wired):
    activity = _channel_activity()
    activity["channelData"] = {"channel": {"id": "19:chan@thread.tacv2"}}
    _post(app, activity)
    assert wired["dispatched"] == []


def test_mention_only_message_with_no_text_is_ignored(app, wired):
    _post(app, _channel_activity(text="<at>Aurora</at>"))
    assert wired["dispatched"] == []


# --- DMs -------------------------------------------------------------------------------

def test_dm_is_answered_without_a_mention_or_team(app, wired):
    resp = _post(app, _dm_activity())
    assert resp.status_code == 200
    (args, kwargs), = wired["dispatched"]
    assert args == ("uid-admin", "what's broken right now?")
    assert kwargs["team_id"] is None
    assert kwargs["channel_id"] == "a:1dm-conversation"
    assert kwargs["conversation_id"] == "a:1dm-conversation"


def test_dm_from_unknown_tenant_is_not_dispatched(app, wired):
    wired["user"] = None
    _post(app, _dm_activity())
    assert wired["dispatched"] == []
