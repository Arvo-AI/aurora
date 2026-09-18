"""Tests for the agent-callable Slack write tool (post_slack_message).

Phase 6: the tool that lets the team-routing agent post a new message or thread
a follow-up under an existing message. Uses the shared FakeSlackClient.
"""

import json
from unittest.mock import patch

import pytest

from tests.utils.notifications.slack_fakes import FakeSlackClient, CHAN

import chat.backend.agent.tools.slack_tool as slack_tool


def _run(client, **kwargs):
    """Invoke post_slack_message with the Slack client patched to `client`."""
    with patch.object(slack_tool, "get_slack_client_for_user", return_value=client):
        return json.loads(slack_tool.post_slack_message(user_id="u1", **kwargs))


def test_new_top_level_post():
    client = FakeSlackClient()
    out = _run(client, channel_id=CHAN, text="db down")
    assert out["status"] == "posted"
    assert out["threaded"] is False
    assert client.sent[0]["thread_ts"] is None
    assert client.sent[0]["text"] == "db down"


def test_threaded_reply_passes_thread_ts():
    client = FakeSlackClient()
    out = _run(client, channel_id=CHAN, text="still happening", thread_ts="1700000000.000001")
    assert out["status"] == "posted"
    assert out["threaded"] is True
    assert client.sent[0]["thread_ts"] == "1700000000.000001"


def test_not_in_channel_joins_and_retries():
    client = FakeSlackClient(not_in_channel_until_joined=True, join_succeeds=True)
    out = _run(client, channel_id=CHAN, text="hi")
    assert out["status"] == "posted"
    # Joined once, then the retry landed the message.
    assert client.joined == [CHAN]
    assert len(client.sent) == 1


def test_not_in_channel_join_fails_returns_error():
    client = FakeSlackClient(not_in_channel_until_joined=True, join_succeeds=True)
    # join "succeeds" but membership check in the fake only flips on join_channel;
    # simulate a persistent not_in_channel by never adding to joined.
    client.join_channel = lambda channel: None  # join returns None → still not a member
    out = _run(client, channel_id=CHAN, text="hi")
    assert "error" in out


def test_empty_text_rejected():
    client = FakeSlackClient()
    out = _run(client, channel_id=CHAN, text="   ")
    assert "error" in out
    assert client.attempts == 0


def test_missing_channel_rejected():
    client = FakeSlackClient()
    out = _run(client, channel_id="", text="hi")
    assert "error" in out
    assert client.attempts == 0


def test_not_connected_returns_error():
    with patch.object(slack_tool, "get_slack_client_for_user", return_value=None):
        out = json.loads(slack_tool.post_slack_message(user_id="u1", channel_id=CHAN, text="hi"))
    assert "error" in out


def test_no_user_returns_error():
    out = json.loads(slack_tool.post_slack_message(channel_id=CHAN, text="hi"))
    assert "error" in out


def test_long_text_is_trimmed_not_rejected():
    client = FakeSlackClient()
    long_text = "x" * (slack_tool._MAX_POST_CHARS + 500)
    out = _run(client, channel_id=CHAN, text=long_text)
    assert out["status"] == "posted"
    assert len(client.sent[0]["text"]) <= slack_tool._MAX_POST_CHARS
