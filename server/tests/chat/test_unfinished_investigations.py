"""An RCA stopped before it finished ends with a partial report, not a failure.

* The time limit, the stall watchdog, an error or a dead worker: if the run
  gathered evidence, it is summarized as an incomplete report.
* The summary falls back to the incident's execution steps when the session's
  context was never saved.
* Only the run that owns the RCA reports it, once: a run that already
  finished, a follow-up chat, or a merged incident never gets a partial report.
* Evidence is the RCA's own session and its sub-agents, never action runs.
* A partial report is not folded into recurrences or the Incident Index, and a
  report that cannot be written is still announced.
* A summary that fails keeps a report an earlier run wrote.
* A partial report updates the cards but is not posted back or routed.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import chat.background.summarization as summarization

SESSION = "44444444-4444-4444-4444-444444444444"
USER = "user-1"
INCIDENT = "55555555-5555-5555-5555-555555555555"


# --- the partial report ------------------------------------------------------------

@pytest.mark.parametrize("reason, words", [
    ("time_limit", "reached its time limit"),
    ("stalled", "stopped making progress"),
])
def test_the_summary_is_told_the_investigation_is_incomplete(reason, words):

    note = summarization._unfinished_investigation_note(reason)
    assert f"Investigation incomplete: it {words}" in note
    assert "Root cause not determined" in note
    assert "Not Checked" in note


TASK = "task-1"


class _Cursor:
    """Answers the claim (owner, status), then the evidence check."""

    def __init__(self, owner, status, has_evidence):
        self.rows = [(owner, status), (has_evidence,)]
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def task(monkeypatch):
    import chat.background.task as task_mod

    calls = SimpleNamespace(cursor=None, statuses=[], enqueued=[])

    def _pool(has_evidence=True, owner=TASK, status="investigating"):
        calls.cursor = _Cursor(owner, status, has_evidence)
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


def _summarize(task_mod, reason="time_limit", run_task_id=TASK):
    return task_mod._summarize_unfinished_investigation(INCIDENT, USER, SESSION, reason, run_task_id=run_task_id)


def test_an_rca_with_evidence_is_summarized_as_incomplete(task):
    task_mod, calls = task
    calls.pool()
    assert _summarize(task_mod) == task_mod.REPORTED
    assert calls.statuses == [("analyzed", "summarizing")]
    assert calls.enqueued == [{
        "incident_id": INCIDENT, "user_id": USER, "session_id": SESSION, "unfinished_reason": "time_limit",
    }]
    claim = [(sql, p) for sql, p in calls.cursor.executed if sql.startswith("UPDATE incidents")]
    # The run gives up its claim, and its stale task id, so the sweep does not
    # read it as a dead worker; the reason is what makes it continuable.
    assert "rca_run_task_id = NULL" in claim[0][0] and "rca_celery_task_id = NULL" in claim[0][0]
    assert claim[0][1] == (True, "time_limit", INCIDENT)


def test_a_run_that_already_finished_is_not_reported_again(task):
    """The time limit firing after the run finalized must not re-mark it unfinished."""
    task_mod, calls = task
    calls.pool(owner=None)  # the finalize already released the claim
    assert _summarize(task_mod) == task_mod.REPORTED  # handled: the caller leaves it be
    assert calls.enqueued == [] and calls.statuses == []
    assert not any(sql.startswith("UPDATE") for sql, _ in calls.cursor.executed)


def test_a_run_that_does_not_own_the_rca_reports_nothing(task):
    task_mod, calls = task
    calls.pool(owner="another-task")
    assert _summarize(task_mod) == task_mod.REPORTED
    assert calls.enqueued == []


def test_a_merged_incident_gets_no_report(task):
    task_mod, calls = task
    calls.pool(status="merged")
    assert _summarize(task_mod) == task_mod.REPORTED
    assert calls.enqueued == []


def test_the_sweep_reports_whichever_run_owned_the_rca(task):
    task_mod, calls = task
    calls.pool(owner="dead-task")
    assert _summarize(task_mod, "interrupted", run_task_id=None) == task_mod.REPORTED
    assert calls.enqueued[0]["unfinished_reason"] == "interrupted"


def test_evidence_is_the_rca_sessions_own(task):
    """An action run's steps (a rollout restart, say) never make a report."""
    task_mod, calls = task
    calls.pool()
    _summarize(task_mod)
    evidence_sql = next(sql for sql, _ in calls.cursor.executed if "FROM execution_steps" in sql)
    assert "es.session_id = rca.aurora_chat_session_id::text" in evidence_sql
    assert "OR session_id" not in evidence_sql
    # A started call with no output, or a coordination call, is not evidence.
    assert "es.tool_output IS NOT NULL AND btrim(es.tool_output) <> ''" in evidence_sql
    params = next(p for sql, p in calls.cursor.executed if "FROM execution_steps" in sql)
    assert "dispatch_subagent" in params[1]


def test_an_rca_with_no_evidence_still_fails(task):
    task_mod, calls = task
    calls.pool(has_evidence=False)
    assert _summarize(task_mod, "stalled") == task_mod.UNSUMMARIZED
    assert calls.statuses == [] and calls.enqueued == []
    # The claim is still released, without a reason to continue.
    claim = [p for sql, p in calls.cursor.executed if sql.startswith("UPDATE incidents")]
    assert claim == [(False, "stalled", INCIDENT)]


def test_a_failure_to_queue_the_summary_falls_back_to_failing(task, monkeypatch):
    task_mod, calls = task
    calls.pool()

    def _broker_down(**kw):
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(summarization.generate_incident_summary_from_chat, "delay", _broker_down)
    assert _summarize(task_mod) == task_mod.UNSUMMARIZED


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
    sql = " ".join(cursor.execute.call_args[0][0].split())
    assert "FROM execution_steps es" in sql and "ORDER BY es.step_index" in sql
    # Only the RCA's session and its sub-agents: not action runs on the incident.
    assert "starts_with(es.session_id, rca.aurora_chat_session_id::text || '::')" in sql


def test_a_failed_step_read_gives_no_citations(monkeypatch):
    from chat.background.citation_extractor import CitationExtractor

    cursor = _steps_pool(monkeypatch, [])
    cursor.execute.side_effect = RuntimeError("db down")
    assert CitationExtractor().extract_citations_from_steps(INCIDENT, USER) == []


# --- a report that cannot be written ------------------------------------------------

def test_an_unwritten_partial_report_is_announced(monkeypatch):
    import utils.notifications.dispatcher as dispatcher
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


# --- what a summary does with the incident's unfinished state ------------------------

def _plan(reason=None, finishes=False, prior=None, continuations=0):
    return summarization._plan_summary(reason, finishes, prior, continuations)


def test_a_finished_rca_writes_a_full_report():
    plan = _plan(finishes=True)
    assert plan.unfinished_reason is None and not plan.keeps_earlier_report


def test_a_follow_up_on_an_unfinished_rca_stays_partial():
    plan = _plan(prior="time_limit")
    assert plan.unfinished_reason == "time_limit"
    assert plan.include_recorded_steps  # over the RCA's evidence, not the follow-up alone
    assert plan.keeps_earlier_report


def test_a_follow_up_on_a_finished_rca_is_unchanged():
    plan = _plan()
    assert plan.unfinished_reason is None and not plan.include_recorded_steps
    assert not plan.keeps_earlier_report


def test_the_first_partial_report_has_nothing_earlier_to_keep():
    plan = _plan(reason="time_limit", prior="time_limit", continuations=0)
    assert plan.unfinished_reason == "time_limit" and not plan.keeps_earlier_report


@pytest.mark.parametrize("reason, finishes", [(None, True), ("time_limit", False)])
def test_a_continuation_keeps_the_earlier_report_if_its_own_fails(reason, finishes):
    plan = _plan(reason=reason, finishes=finishes, prior="time_limit", continuations=1)
    assert plan.keeps_earlier_report


def test_a_finished_rcas_report_clears_its_unfinished_state_in_the_same_write(monkeypatch):

    cursor = MagicMock()
    cursor.rowcount = 1
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    pool = MagicMock()
    pool.get_admin_connection.return_value.__enter__.return_value = conn
    monkeypatch.setattr(summarization, "db_pool", pool)
    monkeypatch.setattr(summarization, "set_rls_context", lambda *a, **k: "org-1")

    assert summarization._update_incident_summary(INCIDENT, "report", user_id=USER, clear_unfinished=True) is True
    sql, params = cursor.execute.call_args_list[0][0]
    assert "rca_unfinished_reason = CASE WHEN %s THEN NULL" in sql
    assert True in params

    cursor.execute.side_effect = RuntimeError("db down")
    assert summarization._update_incident_summary(INCIDENT, "report", user_id=USER) is False


def test_a_failed_summary_keeps_the_earlier_report(monkeypatch):
    import utils.notifications.dispatcher as dispatcher

    cursor = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    pool = MagicMock()
    pool.get_admin_connection.return_value.__enter__.return_value = conn
    monkeypatch.setattr(summarization, "db_pool", pool)
    monkeypatch.setattr(summarization, "set_rls_context", lambda *a, **k: "org-1")
    sent = []
    monkeypatch.setattr(
        dispatcher, "notify_investigation_failed",
        lambda user_id, incident_id, error_message=None: sent.append(error_message),
    )

    summarization._keep_earlier_report(INCIDENT, USER, announce=True)
    sql = cursor.execute.call_args[0][0]
    # Back to its report, still continuable: no summary or reason is touched.
    assert "aurora_status = 'complete'" in sql
    assert "aurora_summary" not in sql and "rca_unfinished_reason" not in sql
    assert sent and "earlier report is kept" in sent[0]

    sent.clear()
    summarization._keep_earlier_report(INCIDENT, USER, announce=False)  # a follow-up's
    assert sent == []


# --- notifications for a partial report ----------------------------------------------

@pytest.fixture
def dispatch(monkeypatch):
    import utils.notifications.dispatcher as dispatcher
    import utils.notifications.pagerduty_notification_service as pd
    import utils.notifications.team_routing as routing

    sent = []
    monkeypatch.setattr(dispatcher, "get_org_id_for_user", lambda u: "org-1")
    monkeypatch.setattr(dispatcher, "get_org_preference", lambda org, key, default=None: True)
    monkeypatch.setattr(dispatcher, "get_user_preference", lambda user, key, default=None: True)
    monkeypatch.setattr(dispatcher, "_has_slack_connected", lambda u: True)
    monkeypatch.setattr(dispatcher, "_has_teams_connected", lambda u: False)
    monkeypatch.setattr(dispatcher, "_has_google_chat_connected", lambda u: False)
    monkeypatch.setattr(dispatcher, "_send_emails", lambda *a, **k: sent.append("email"))
    import utils.notifications.slack_notification_service as slack
    monkeypatch.setattr(slack, "send_slack_investigation_completed_notification",
                        lambda *a, **k: sent.append("slack"))
    monkeypatch.setattr(pd, "send_pagerduty_incident_note", lambda *a, **k: sent.append("pagerduty"))
    monkeypatch.setattr(routing, "trigger_team_routing_agent", lambda *a, **k: sent.append("routing"))

    def _run(unfinished_reason):
        monkeypatch.setattr(dispatcher, "_get_incident_data", lambda i, u: {
            "incident_id": INCIDENT, "source_type": "pagerduty", "unfinished_reason": unfinished_reason,
        })
        sent.clear()
        dispatcher.notify_investigation_completed(USER, INCIDENT)
        return list(sent)

    return _run


def test_a_partial_report_updates_the_cards_only(dispatch):
    sent = dispatch("time_limit")
    assert "slack" in sent and "email" in sent
    assert "pagerduty" not in sent and "routing" not in sent


def test_a_finished_report_is_posted_back_and_routed(dispatch):
    sent = dispatch(None)
    assert {"slack", "email", "pagerduty", "routing"} <= set(sent)


def test_an_unreadable_unfinished_state_raises_so_the_summary_retries(monkeypatch):
    """Read as "finished", a failed lookup would turn a partial report into a conclusion."""
    pool = MagicMock()
    pool.get_admin_connection.side_effect = RuntimeError("db down")
    monkeypatch.setattr(summarization, "db_pool", pool)
    with pytest.raises(RuntimeError):
        summarization._read_unfinished_state(INCIDENT, USER)
