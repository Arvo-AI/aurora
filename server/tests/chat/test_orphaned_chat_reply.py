"""A killed worker never reaches its finally block, so the stall sweep has to
post the error that replaces Thinking….
"""

import sys
import types
from unittest.mock import MagicMock

import utils.cache.redis_client as redis_client

# task.py refuses to import without Redis. Leave a live client alone.
if redis_client.get_redis_client() is None:
    redis_client.get_redis_client = lambda: MagicMock(name="redis")

import chat.background.task as task

SESSION = "11111111-1111-1111-1111-111111111111"


def test_killed_worker_replaces_thinking_even_when_a_draft_was_saved(monkeypatch):
    slack_helpers = types.ModuleType("routes.slack.slack_events_helpers")
    slack_helpers.format_response_for_slack = lambda text: text
    monkeypatch.setitem(sys.modules, "routes.slack.slack_events_helpers", slack_helpers)
    monkeypatch.setenv("FRONTEND_URL", "https://aurora.example.com")

    from tests.utils.notifications.slack_fakes import CHAN, FakeSlackClient

    pool_touched = {"yes": False}

    def boom_pool():
        pool_touched["yes"] = True
        raise AssertionError("a forced error must not read the saved draft")

    monkeypatch.setattr(task.db_pool, "get_admin_connection", boom_pool)
    client = FakeSlackClient()
    monkeypatch.setattr(
        "connectors.slack_connector.client.get_slack_client_for_user",
        lambda _uid: client,
    )

    sent = task._send_response_to_slack(
        "u1",
        SESSION,
        {"source": "slack", "channel": CHAN, "thinking_message_ts": "1.1"},
        force_text=task._WORKER_LOST_REPLY,
    )

    assert sent is True
    assert pool_touched["yes"] is False
    assert client.updated[-1]["text"].startswith(task._WORKER_LOST_REPLY)
    assert client.updated[-1]["ts"] == "1.1"


def test_sweep_posts_the_error_to_the_chat_that_asked(monkeypatch):
    calls = []
    monkeypatch.setattr(task, "_send_response_to_slack", lambda *a, **k: calls.append(("slack", k)))
    monkeypatch.setattr(task, "_send_response_to_google_chat", lambda *a, **k: calls.append(("gchat", k)))
    monkeypatch.setattr(task, "_send_response_to_teams", lambda *a, **k: calls.append(("teams", k)))

    task._notify_orphaned_chat_reply("u1", SESSION, {
        "triggerMetadata": {"source": "slack", "channel": "C1", "thinking_message_ts": "1.2"},
    })
    task._notify_orphaned_chat_reply("u1", SESSION, {
        "triggerMetadata": {"source": "google_chat", "space_name": "spaces/1"},
    })
    task._notify_orphaned_chat_reply("u1", SESSION, {
        "triggerMetadata": {"source": "teams", "channel_id": "19:x"},
    })
    task._notify_orphaned_chat_reply("u1", SESSION, {"triggerMetadata": {"source": "grafana"}})

    assert [name for name, _k in calls] == ["slack", "gchat", "teams"]
    assert all(k["force_text"] == task._WORKER_LOST_REPLY for _n, k in calls)


def test_a_failed_chat_post_does_not_abort_the_sweep(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("slack down")

    monkeypatch.setattr(task, "_send_response_to_slack", boom)
    task._notify_orphaned_chat_reply(
        "u1", SESSION, {"triggerMetadata": {"source": "slack", "channel": "C1"}},
    )


def test_claim_only_flips_sessions_still_in_progress():
    cursor = MagicMock()
    cursor.fetchall.return_value = [(SESSION, {"triggerMetadata": {"source": "slack"}})]
    rows = task._claim_orphaned_sessions(cursor, "id = %s AND status = 'in_progress'", (SESSION,))
    sql, params = cursor.execute.call_args.args
    assert "status = 'in_progress'" in sql
    assert "jsonb_typeof(messages) = 'array'" in sql
    assert params[0] == task._WORKER_LOST_REPLY
    assert params[1] == SESSION
    assert rows[0][0] == SESSION
