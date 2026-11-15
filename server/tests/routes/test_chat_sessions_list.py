"""GET /chat_api/sessions must count messages in SQL, not ship them.

Selecting the whole ``messages`` JSONB for every session just to take its
length made the list cost proportional to total chat history.
"""

from datetime import datetime
from unittest.mock import MagicMock

import pytest
from flask import Flask



@pytest.fixture
def run_list(monkeypatch):
    # config.rate_limiting refuses to import without a Redis URL
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    import routes.chat_routes as chat_routes

    def _run(rows, query_string=""):
        cursor = MagicMock()
        cursor.fetchall.return_value = rows
        conn = MagicMock()
        conn.cursor.return_value = cursor
        monkeypatch.setattr(chat_routes, "connect_to_db_as_user", lambda: conn)
        monkeypatch.setattr(chat_routes, "set_rls_context", lambda *a, **k: None)
        monkeypatch.setattr(chat_routes, "get_org_id_from_request", lambda: "org-1")

        # Strip the limiter and RBAC wrappers (the real limiter is installed locally)
        handler = chat_routes.get_chat_sessions
        while hasattr(handler, "__wrapped__"):
            handler = handler.__wrapped__
        app = Flask(__name__)
        with app.test_request_context(f"/sessions{query_string}"):
            response, status = handler("uid-self")
        return response.get_json(), status, cursor

    return _run


def _row(count, owner="uid-self"):
    now = datetime(2026, 1, 1)
    return ("sess-1", "Title", now, now, count, {}, "active", owner, None)


@pytest.mark.parametrize("query", ["", "?scope=org"])
def test_query_counts_in_sql_and_never_selects_messages(run_list, query):
    _, _, cursor = run_list([], query)

    sql = cursor.execute.call_args.args[0]
    assert "jsonb_array_length(cs.messages)" in sql
    assert "THEN jsonb_array_length" in sql
    # The payload column must not be selected as a bare value
    assert "ELSE cs.messages END" not in sql


def test_message_count_comes_from_the_row(run_list):
    body, status, _ = run_list([_row(7), _row(0)])

    assert status == 200
    assert [s["message_count"] for s in body["sessions"]] == [7, 0]


def test_null_count_is_zero(run_list):
    body, _, _ = run_list([_row(None)])

    assert body["sessions"][0]["message_count"] == 0
