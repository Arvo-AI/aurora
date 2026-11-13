"""An RCA stopped before it finished ends with a partial report, not a failure.

* The time limit, the stall watchdog, an error or a dead worker: if the run
  gathered evidence, it is summarized as an incomplete report.
* The summary falls back to the incident's execution steps when the session's
  context was never saved.
* A partial report is not folded into recurrences or the Incident Index, and a
  report that cannot be written is still announced.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

SESSION = "44444444-4444-4444-4444-444444444444"
USER = "user-1"
INCIDENT = "55555555-5555-5555-5555-555555555555"


# --- the partial report ------------------------------------------------------------

@pytest.mark.parametrize("reason, words", [
    ("time_limit", "reached its time limit"),
    ("stalled", "stopped making progress"),
])
def test_the_summary_is_told_the_investigation_is_incomplete(reason, words):
    import chat.background.summarization as summarization

    note = summarization._unfinished_investigation_note(reason)
    assert f"Investigation incomplete: it {words}" in note
    assert "Root cause not determined" in note
    assert "Not Checked" in note


class _Cursor:
    def __init__(self, has_evidence):
        self.has_evidence = has_evidence
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return (self.has_evidence,)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def task(monkeypatch):
    import chat.background.task as task_mod
    import chat.background.summarization as summarization

    calls = SimpleNamespace(cursor=None, statuses=[], enqueued=[])

    def _pool(has_evidence):
        calls.cursor = _Cursor(has_evidence)
        conn = MagicMock()
        conn.cursor.return_value = calls.cursor
        pool = MagicMock()
        pool.get_admin_connection.return_value.__enter__.return_value = conn
        monkeypatch.setattr(task_mod, "db_pool", pool)

    calls.pool = _pool
    monkeypatch.setattr(task_mod, "set_rls_context", lambda *a, **k: "org-1")
    monkeypatch.setattr(
        task_mod, "_update_incident_status_and_aurora",
        lambda incident_id, status, aurora, user_id=None: calls.statuses.append((status, aurora)),
    )
    monkeypatch.setattr(
        summarization.generate_incident_summary_from_chat, "delay",
        lambda **kw: calls.enqueued.append(kw),
    )
    return task_mod, calls


def test_an_rca_with_evidence_is_summarized_as_incomplete(task):
    task_mod, calls = task
    calls.pool(has_evidence=True)
    assert task_mod._summarize_unfinished_investigation(INCIDENT, USER, SESSION, "time_limit") is True
    assert calls.statuses == [("analyzed", "summarizing")]
    assert calls.enqueued == [{
        "incident_id": INCIDENT, "user_id": USER, "session_id": SESSION, "unfinished_reason": "time_limit",
    }]
    # The stale task id is cleared so the sweep does not read the run as a dead worker.
    assert any("rca_celery_task_id = NULL" in sql for sql, _ in calls.cursor.executed)


def test_an_rca_with_no_evidence_still_fails(task):
    task_mod, calls = task
    calls.pool(has_evidence=False)
    assert task_mod._summarize_unfinished_investigation(INCIDENT, USER, SESSION, "stalled") is False
    assert calls.statuses == [] and calls.enqueued == []


def test_a_failure_to_queue_the_summary_falls_back_to_failing(task, monkeypatch):
    task_mod, calls = task
    calls.pool(has_evidence=True)
    import chat.background.summarization as summarization

    def _broker_down(**kw):
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(summarization.generate_incident_summary_from_chat, "delay", _broker_down)
    assert task_mod._summarize_unfinished_investigation(INCIDENT, USER, SESSION, "time_limit") is False


# --- evidence when the session context was never saved ----------------------------

def _steps_pool(monkeypatch, rows):
    import utils.db.connection_pool as pool_mod
    import utils.auth.stateless_auth as auth

    cursor = MagicMock()
    cursor.fetchall.return_value = rows
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    pool = MagicMock()
    pool.get_admin_connection.return_value.__enter__.return_value = conn
    monkeypatch.setattr(pool_mod, "db_pool", pool)
    monkeypatch.setattr(auth, "set_rls_context", lambda *a, **k: "org-1")
    return cursor


def test_citations_are_rebuilt_from_execution_steps(monkeypatch):
    from chat.background.citation_extractor import CitationExtractor

    cursor = _steps_pool(monkeypatch, [
        ("cloud_exec", "c1", {"command": "kubectl get pods -n api"}, "pod-a CrashLoopBackOff", None),
        ("dispatch_subagent", "c2", {}, "rollup", None),  # coordination, not evidence
        ("query_datadog", "c3", '{"query": "errors"}', "   ", None),  # empty output
        ("query_datadog", "c4", '{"query": "errors"}', "500s spiked at 13:40", None),
    ])
    citations = CitationExtractor().extract_citations_from_steps(INCIDENT, USER)

    assert [c.index for c in citations] == [1, 2]
    assert [c.tool_call_id for c in citations] == ["c1", "c4"]
    assert "CrashLoopBackOff" in citations[0].output
    sql = cursor.execute.call_args[0][0]
    assert "FROM execution_steps" in sql and "ORDER BY step_index" in sql


def test_a_failed_step_read_gives_no_citations(monkeypatch):
    from chat.background.citation_extractor import CitationExtractor

    cursor = _steps_pool(monkeypatch, [])
    cursor.execute.side_effect = RuntimeError("db down")
    assert CitationExtractor().extract_citations_from_steps(INCIDENT, USER) == []


# --- a report that cannot be written ------------------------------------------------

def test_an_unwritten_partial_report_is_announced(monkeypatch):
    import utils.notifications.dispatcher as dispatcher
    import chat.background.summarization as summarization
    _notify_unwritten_partial_report = summarization._notify_unwritten_partial_report

    sent = []
    monkeypatch.setattr(
        dispatcher, "notify_investigation_failed",
        lambda user_id, incident_id, error_message=None: sent.append((incident_id, error_message)),
    )
    _notify_unwritten_partial_report(USER, INCIDENT, "time_limit")
    assert sent and sent[0][0] == INCIDENT and "stopped early" in sent[0][1]

    sent.clear()
    _notify_unwritten_partial_report(USER, INCIDENT, None)  # a normal summary failing: unchanged
    assert sent == []
