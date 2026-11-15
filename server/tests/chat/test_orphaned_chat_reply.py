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


# --- the sweep needs the placeholder's id on the session ---------------------------

class _RecordingPool:
    def __init__(self):
        self.cursor = MagicMock()
        self.conn = MagicMock()
        self.conn.cursor.return_value.__enter__.return_value = self.cursor

    def get_admin_connection(self):
        cm = MagicMock()
        cm.__enter__.return_value = self.conn
        return cm


def test_run_stores_the_placeholder_id_where_the_sweep_reads_it(monkeypatch):
    """The Slack handler creates the session, then posts Thinking…, so only the
    task knows the placeholder's ts. Found by killing a worker mid-reply: the
    sweep posted a second message and left Thinking… in the thread."""
    import json

    pool = _RecordingPool()
    monkeypatch.setattr(task, "db_pool", pool)
    monkeypatch.setattr(task, "set_rls_context", lambda *a, **k: "org-1")
    meta = {"source": "slack", "channel": "C1", "thread_ts": "1.0", "thinking_message_ts": "2.0"}

    task._remember_reply_target(SESSION, "user-1", meta)

    sql, (payload, session_id) = pool.cursor.execute.call_args.args
    assert "'{triggerMetadata}'" in sql and "|| %s::jsonb" in sql
    assert json.loads(payload)["thinking_message_ts"] == "2.0"
    assert session_id == SESSION
    pool.conn.commit.assert_called_once()


def test_non_chat_runs_do_not_copy_their_trigger_onto_the_session(monkeypatch):
    pool = _RecordingPool()
    monkeypatch.setattr(task, "db_pool", pool)
    task._remember_reply_target(SESSION, "user-1", {"source": "pagerduty", "payload": "x" * 10_000})
    pool.cursor.execute.assert_not_called()


def test_a_failure_to_store_it_does_not_stop_the_run(monkeypatch):
    def _down():
        raise RuntimeError("database restarting")

    monkeypatch.setattr(task.db_pool, "get_admin_connection", _down)
    task._remember_reply_target(SESSION, "user-1", {"source": "teams", "thinking_activity_id": "a1"})


def test_the_reply_target_is_stored_before_the_workflow_starts():
    import ast
    from pathlib import Path

    tree = ast.parse(Path(task.__file__).read_text(encoding="utf-8"))
    func = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "run_background_chat")
    lines = {
        getattr(n.func, "id", None): n.lineno
        for n in ast.walk(func) if isinstance(n, ast.Call) and getattr(n.func, "id", None)
        in ("_remember_reply_target", "run_watched")
    }
    assert lines["_remember_reply_target"] < lines["run_watched"]
