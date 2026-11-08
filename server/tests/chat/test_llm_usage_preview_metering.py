"""PR preview usage is stored for the operator but never metered to the user.

``LLMUsageTracker.store_usage`` writes every LLM call to ``llm_usage_tracking``
and then reports it to the ``report_usage`` hook (marketplace metering on
hosted deployments). A no-install PR preview is free for the user, so its
calls must skip the hook, and nothing else may.
"""

from unittest.mock import MagicMock

import pytest

from chat.backend.agent.utils import llm_usage_tracker as tracker_mod
from chat.backend.agent.utils.llm_usage_tracker import LLMUsage, LLMUsageTracker

SESSION_ID = "11111111-2222-3333-4444-555555555555"
PREVIEW = {"source": "change_gating", "preview": True, "repo": "acme/widgets"}


class _Cursor:
    def __init__(self, trigger_metadata=None, lookup_error=None):
        self.trigger_metadata = trigger_metadata
        self.lookup_error = lookup_error
        self.sql = []
        self.statements = []  # first two words of each statement, e.g. "INSERT INTO"
        self._last = ""

    def execute(self, sql, params=None):
        words = sql.split()
        self.sql.append(sql)
        self.statements.append(f"{words[0]} {words[1]}".upper())
        self._last = sql
        if "FROM chat_sessions" in sql and self.lookup_error:
            raise self.lookup_error

    def fetchone(self):
        if "FROM chat_sessions" in self._last:
            return (self.trigger_metadata,) if self.trigger_metadata is not None else None
        return None


@pytest.fixture
def store(monkeypatch):
    """Return a runner: store one usage row against a fake DB, report what happened."""
    report = MagicMock()
    monkeypatch.setattr(tracker_mod, "set_rls_context", lambda *a, **k: "org-1")
    monkeypatch.setattr("utils.hooks.get_hook", lambda name: report)
    monkeypatch.setattr("utils.billing.billing_cache.clear_user_cache", lambda *a, **k: None)

    def run(cursor, session_id=SESSION_ID):
        conn = MagicMock()
        conn.cursor.return_value = cursor
        pool = MagicMock()
        pool.get_user_connection.return_value.__enter__ = lambda s: conn
        pool.get_user_connection.return_value.__exit__ = lambda s, *a: False
        monkeypatch.setattr(tracker_mod, "db_pool", pool)
        usage = LLMUsage(
            user_id="user-1", session_id=session_id, model_name="m", api_provider="p",
            request_type="chat", input_tokens=10, output_tokens=5, estimated_cost=0.01,
            response_time_ms=100,
        )
        stored = LLMUsageTracker.store_usage(usage)
        return stored, report, conn

    return run


def test_preview_usage_is_stored_but_not_metered(store):
    cursor = _Cursor(trigger_metadata=PREVIEW)
    stored, report, conn = store(cursor)

    assert stored is True
    assert "INSERT INTO" in cursor.statements
    conn.commit.assert_called_once()
    report.assert_not_called()


@pytest.mark.parametrize(
    "trigger_metadata",
    [
        {"source": "grafana"},
        {"source": "change_gating"},  # the installed PR review stays metered
        None,  # interactive chat: no trigger metadata
    ],
)
def test_every_other_session_is_metered(store, trigger_metadata):
    stored, report, _ = store(_Cursor(trigger_metadata=trigger_metadata))
    assert stored is True
    report.assert_called_once()
    assert report.call_args.args[:2] == ("org-1", 0.01)


@pytest.mark.parametrize("session_id", [None, "", "not-a-uuid"])
def test_sessions_that_cannot_be_previews_skip_the_lookup(store, session_id):
    cursor = _Cursor(trigger_metadata=PREVIEW)
    _, report, _ = store(cursor, session_id=session_id)
    assert not any("FROM chat_sessions" in s for s in cursor.sql)
    assert "SAVEPOINT PREVIEW_METERING" not in cursor.statements
    report.assert_called_once()


def test_failed_lookup_keeps_the_row_and_meters_as_usual(store):
    cursor = _Cursor(lookup_error=RuntimeError("db hiccup"))
    stored, report, conn = store(cursor)

    assert stored is True
    assert "ROLLBACK TO" in cursor.statements
    conn.commit.assert_called_once()
    report.assert_called_once()
