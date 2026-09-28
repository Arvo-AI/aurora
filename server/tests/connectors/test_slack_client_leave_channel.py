"""SlackClient.leave_channel: what counts as "Aurora is out of the channel".

Membership is Aurora's source of truth for "active", so the deactivate route
only prunes a channel's row once leave_channel confirms the bot is out. These
tests pin the True/False contract that decision relies on.
"""

from unittest.mock import patch

from connectors.slack_connector.client import SlackAPIError, SlackClient


def _client_raising(error):
    client = SlackClient("xoxb-test")

    def fake_request(method, endpoint, data=None, **kwargs):
        assert (method, endpoint) == ("POST", "conversations.leave")
        raise SlackAPIError(error)

    return client, fake_request


def test_successful_leave_returns_true():
    client = SlackClient("xoxb-test")
    with patch.object(client, "_make_request", return_value={"ok": True}):
        assert client.leave_channel("C1") is True


def test_already_not_a_member_counts_as_left():
    # These Slack errors all mean the bot isn't in the channel anymore, so it's
    # safe to treat as "left" and let the caller prune its row.
    for err in ("channel_not_found", "not_in_channel", "is_archived", "already_left"):
        client, req = _client_raising(err)
        with patch.object(client, "_make_request", side_effect=req):
            assert client.leave_channel("C1") is True, err


def test_cant_leave_general_returns_false():
    # #general can't be left — Aurora is still a member, so the caller must keep
    # the row (else the next reconcile re-adds it and the channel flaps).
    client, req = _client_raising("cant_leave_general")
    with patch.object(client, "_make_request", side_effect=req):
        assert client.leave_channel("C_GENERAL") is False


def test_transport_error_returns_false():
    # A transport failure may or may not have landed — assume still a member.
    client = SlackClient("xoxb-test")
    with patch.object(client, "_make_request", side_effect=ValueError("boom")):
        assert client.leave_channel("C1") is False
