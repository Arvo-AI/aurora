"""Teams agent tools: every read/post is limited to Aurora's active channels."""

import json
from unittest.mock import MagicMock

import pytest

from chat.backend.agent.tools import teams_tool as tt
from services.channels import registry


@pytest.fixture
def graph(monkeypatch):
    """Fake Graph client plus a switchable channel-membership answer."""
    state = {"member": True, "client": MagicMock(), "membership_calls": []}
    state["client"].list_channel_messages.return_value = [
        {"id": "m1", "createdDateTime": "2026-10-16T12:00:00Z", "body": {"content": "hi"},
         "from": {"user": {"displayName": "Pat"}}},
    ]
    state["client"].list_message_replies.return_value = []

    def membership(user_id, provider, channel_id):
        state["membership_calls"].append((provider, channel_id))
        return state["member"]

    monkeypatch.setattr(registry, "channel_membership", membership)
    monkeypatch.setattr(tt, "get_teams_client_for_user", lambda user_id: state["client"])
    return state


def _call(fn, **kwargs):
    return json.loads(fn(user_id="uid-self", **kwargs))


# --- history ---------------------------------------------------------------------------

def test_history_reads_an_active_channel(graph):
    out = _call(tt.get_teams_channel_history, team_id="t1", channel_id="19:c@thread.tacv2")
    assert out["status"] == "ok" and out["count"] == 1


def test_history_refuses_a_channel_that_is_not_active(graph):
    graph["member"] = False
    out = _call(tt.get_teams_channel_history, team_id="t1", channel_id="19:hidden@thread.tacv2")
    assert out["code"] == "channel_not_active"
    graph["client"].list_channel_messages.assert_not_called()


def test_history_fails_closed_when_membership_cannot_be_checked(graph):
    graph["member"] = None
    out = _call(tt.get_teams_channel_history, team_id="t1", channel_id="19:c@thread.tacv2")
    assert out["code"] == "membership_check_failed"
    graph["client"].list_channel_messages.assert_not_called()


def test_history_checks_the_clean_channel_id(graph):
    _call(tt.get_teams_channel_history, team_id="t1", channel_id="19:c@thread.tacv2;messageid=55")
    assert graph["membership_calls"] == [("teams", "19:c@thread.tacv2")]
    assert graph["client"].list_channel_messages.call_args.args[1] == "19:c@thread.tacv2"


# --- thread replies --------------------------------------------------------------------------

def test_thread_replies_read_an_active_channel(graph):
    out = _call(tt.get_teams_thread_replies, team_id="t1", channel_id="19:c@thread.tacv2", message_id="55")
    assert out["status"] == "ok"


def test_thread_replies_refuse_a_channel_that_is_not_active(graph):
    graph["member"] = False
    out = _call(tt.get_teams_thread_replies, team_id="t1", channel_id="19:hidden@thread.tacv2", message_id="55")
    assert out["code"] == "channel_not_active"
    graph["client"].list_message_replies.assert_not_called()


def test_thread_replies_need_all_ids(graph):
    out = _call(tt.get_teams_thread_replies, team_id="t1", channel_id="", message_id="55")
    assert "required" in out["error"]


# --- post ------------------------------------------------------------------------------------

def test_post_refuses_a_channel_that_is_not_active(graph, monkeypatch):
    graph["member"] = False
    out = _call(tt.post_teams_message, team_id="t1", channel_id="19:hidden@thread.tacv2", text="hi")
    assert out["code"] == "channel_not_active"


def test_post_sends_a_threaded_reply_through_the_bot(graph, monkeypatch):
    import connectors.teams_connector.bot_client as bc

    sent = {}
    monkeypatch.setattr(bc, "tenant_id_for_aurora_user", lambda user_id: "tenant-1")
    monkeypatch.setattr(bc, "send_message_to_team_channel", lambda **kw: sent.update(kw) or {"id": "act-9"})
    out = _call(tt.post_teams_message, team_id="t1", channel_id="19:c@thread.tacv2", text="follow-up", reply_to_id="55")
    assert out["status"] == "posted" and out["threaded"] is True and out["id"] == "act-9"
    assert sent["reply_to_id"] == "55" and sent["channel_id"] == "19:c@thread.tacv2"


# --- connected check ---------------------------------------------------------------------------

def test_is_teams_connected_does_not_call_graph_or_refresh(monkeypatch):
    calls = []
    monkeypatch.setattr(tt, "has_teams_credentials", lambda user_id: calls.append(user_id) or True)
    monkeypatch.setattr(tt, "get_teams_client_for_user", lambda user_id: pytest.fail("must not build a client"))
    assert tt.is_teams_connected("uid-self") is True
    assert calls == ["uid-self"]
