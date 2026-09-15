"""SlackClient.get_message: what counts as "Slack no longer has it"."""

from unittest.mock import patch

import pytest

from connectors.slack_connector.client import SlackAPIError, SlackClient

TS = "1700000000.000001"


def _client_answering(payload=None, error=None):
    client = SlackClient("xoxb-test")

    def fake_request(method, endpoint, data=None, timeout=30, max_retries=3):
        assert (method, endpoint) == ("GET", "conversations.replies")
        assert data == {"channel": "C1", "ts": TS, "limit": 1}
        if error:
            raise SlackAPIError(error)
        return payload

    return client, fake_request


def test_present_message_is_returned():
    client, req = _client_answering({"ok": True, "messages": [{"ts": TS, "text": "hi", "reply_count": 2}]})
    with patch.object(client, "_make_request", side_effect=req):
        assert client.get_message("C1", TS)["reply_count"] == 2


def test_thread_not_found_is_gone():
    client, req = _client_answering(error="thread_not_found")
    with patch.object(client, "_make_request", side_effect=req):
        assert client.get_message("C1", TS) is None


def test_tombstone_of_a_deleted_parent_with_replies_is_gone():
    # Slack keeps "This message was deleted." at the same ts when the parent had replies.
    client, req = _client_answering({"ok": True, "messages": [
        {"ts": TS, "subtype": "tombstone", "text": "This message was deleted.", "reply_count": 3},
    ]})
    with patch.object(client, "_make_request", side_effect=req):
        assert client.get_message("C1", TS) is None


def test_other_ts_in_reply_is_gone():
    client, req = _client_answering({"ok": True, "messages": [{"ts": "1700000000.000009"}]})
    with patch.object(client, "_make_request", side_effect=req):
        assert client.get_message("C1", TS) is None


def test_other_rejections_propagate():
    client, req = _client_answering(error="not_in_channel")
    with patch.object(client, "_make_request", side_effect=req):
        with pytest.raises(SlackAPIError) as exc:
            client.get_message("C1", TS)
        assert exc.value.error == "not_in_channel"
