"""Aurora's Slack reply must always link back to the session that produced it.

Slack clips long answers, and without the link there is no way to find the run
again in the console.
"""

import json
import sys
import types
from unittest.mock import patch

import pytest

from tests.utils.notifications.slack_fakes import CHAN, FakePool, FakeSlackClient

SESSION_ID = "11111111-1111-1111-1111-111111111111"
FRONTEND = "https://aurora.example.com"
EXPECTED_LINK = f"<{FRONTEND}/chat?sessionId={SESSION_ID}|Open the full response in Aurora>"
# Slack rejects chat.update payloads above ~3000 chars.
SLACK_HARD_LIMIT = 3000


@pytest.fixture
def send(monkeypatch):
    """Run _send_response_to_slack with the DB, Slack client and helpers stubbed."""
    helpers = types.ModuleType("routes.slack.slack_events_helpers")
    helpers.format_response_for_slack = lambda text: text
    monkeypatch.setitem(sys.modules, "routes.slack.slack_events_helpers", helpers)
    # Trailing slash on purpose — the link builder must not emit a double slash.
    monkeypatch.setenv("FRONTEND_URL", FRONTEND + "/")

    import chat.background.task as task

    pool = FakePool()
    monkeypatch.setattr(task, "db_pool", pool.pool)
    monkeypatch.setattr(task, "set_rls_context", lambda *a, **k: "org-1")

    def _send(answer: str) -> str:
        pool.cursor.fetchone.return_value = (
            json.dumps([{"sender": "bot", "text": answer}]),
        )
        client = FakeSlackClient()
        with patch(
            "connectors.slack_connector.client.get_slack_client_for_user",
            return_value=client,
        ):
            sent = task._send_response_to_slack(
                "u1",
                SESSION_ID,
                {"source": "slack", "channel": CHAN, "thinking_message_ts": "1.1"},
            )
        assert sent is True
        return client.updated[-1]["text"]

    return _send


def test_short_reply_links_back_to_the_session(send):
    assert send("No packet loss on the vhub.").endswith(EXPECTED_LINK)


def test_truncated_reply_keeps_the_link_and_still_fits_slack(send):
    text = send("x" * 10_000)
    assert text.endswith(EXPECTED_LINK)
    assert "truncated" in text
    assert len(text) < SLACK_HARD_LIMIT


def test_cut_inside_a_code_block_still_renders_the_link(send):
    # An unclosed fence makes Slack render the rest of the message as code,
    # so the link would arrive as plain text instead of something clickable.
    text = send("Here are the logs:\n```\n" + "connection reset\n" * 1_000)
    assert text.count("```") % 2 == 0
    assert text.endswith(EXPECTED_LINK)
    assert len(text) < SLACK_HARD_LIMIT
