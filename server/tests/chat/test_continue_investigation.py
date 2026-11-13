"""Continuing an investigation that stopped before it finished.

* A user can continue an unfinished RCA: it runs again in the same session as
  the incident owner, with a prompt to pick up where it stopped.
* It cannot be continued while running, once finished, when merged, or past
  the cap; a failed enqueue puts the incident back.
* The Slack card offers the button only while continuations are left.
* A continuation that cannot be summarized leaves the earlier report in place.
* A dead worker's RCA with evidence becomes a partial report, not a failure.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from chat.background import continuation
from chat.background.continuation import (
    CONTINUE_SOURCE,
    MAX_CONTINUATIONS,
    ContinueRefused,
    continuation_prompt,
    continue_investigation,
    is_continuation,
)

INCIDENT = "55555555-5555-5555-5555-555555555555"
SESSION = "44444444-4444-4444-4444-444444444444"
OWNER = "owner-1"
CLICKER = "clicker-1"


class _Cursor:
    def __init__(self, row):
        self.row = row
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.row

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def db(monkeypatch):
    """Install a fake pool whose incident row the test sets."""
    state = SimpleNamespace(cursors=[], enqueued=[], rls=True, rls_orgs=[])

    def _pool(row):
        def _conn():
            cursor = _Cursor(row)
            state.cursors.append(cursor)
            conn = MagicMock()
            conn.cursor.return_value = cursor
            ctx = MagicMock()
            ctx.__enter__.return_value = conn
            return ctx

        pool = MagicMock()
        pool.get_admin_connection.side_effect = _conn
        monkeypatch.setattr(continuation, "db_pool", pool)

    state.pool = _pool
    def _rls(*a, **k):
        state.rls_orgs.append(k.get("org_id"))
        return "org-1" if state.rls else None

    monkeypatch.setattr(continuation, "set_rls_context", _rls)

    import chat.background.task as task_mod
    monkeypatch.setattr(task_mod.run_background_chat, "delay", lambda **kw: state.enqueued.append(kw))
    return state


def _row(aurora_status="complete", status="analyzed", reason="time_limit", used=0, session=SESSION):
    return (OWNER, session, aurora_status, status, reason, used, "Root cause not determined. Not Checked: db.")


def _sql(state):
    return [sql for c in state.cursors for sql, _ in c.executed]


# --- starting a continuation ---------------------------------------------------------

def test_an_unfinished_rca_is_continued_in_its_own_session(db):
    db.pool(_row(used=1))
    result = continue_investigation(INCIDENT, CLICKER, "org-1")

    assert result == {"continuationsLeft": MAX_CONTINUATIONS - 2}
    assert len(db.enqueued) == 1
    job = db.enqueued[0]
    # Runs as the owner, in the RCA session, without a second "started" card.
    assert job["user_id"] == OWNER
    assert job["session_id"] == SESSION
    assert job["incident_id"] == INCIDENT
    assert job["send_notifications"] is False
    assert job["mode"] == "ask"
    # Aurora's own report is not checked as user input; only the fixed instruction is.
    assert job["rail_text"] == continuation.CONTINUE_RAIL_TEXT
    assert job["trigger_metadata"] == {"source": CONTINUE_SOURCE, "requested_by": CLICKER}
    assert "reached its time limit" in job["initial_message"]
    assert "Root cause not determined. Not Checked: db." in job["initial_message"]
    sql = _sql(db)
    assert any("FOR UPDATE" in s for s in sql)
    assert any("aurora_status = 'running'" in s and "rca_continuations + 1" in s for s in sql)
    # RLS uses the org the caller was authorized in, not one re-resolved from the user.
    assert db.rls_orgs == ["org-1"]
    # The stopped run's session is reopened, so the heartbeat keeps the continuation alive.
    assert any("UPDATE chat_sessions" in s and "status = 'in_progress'" in s and "heartbeat_at = NULL" in s
               for s in sql)


@pytest.mark.parametrize("row, words, status", [
    (None, "not found", 404),
    (_row(status="merged"), "merged", 409),
    (_row(aurora_status="running"), "already running", 409),
    (_row(aurora_status="summarizing"), "already running", 409),
    (_row(reason=None), "already finished", 409),
    (_row(session=None), "already finished", 409),
    (_row(used=MAX_CONTINUATIONS), "already continued", 409),
])
def test_what_cannot_be_continued(db, row, words, status):
    db.pool(row)
    with pytest.raises(ContinueRefused, match=words) as refused:
        continue_investigation(INCIDENT, CLICKER, "org-1")
    assert refused.value.status == status
    assert db.enqueued == []
    assert not any("rca_continuations + 1" in s for s in _sql(db))


def test_another_orgs_incident_is_not_found(db):
    db.pool(_row())
    db.rls = False
    with pytest.raises(ContinueRefused, match="not found"):
        continue_investigation(INCIDENT, CLICKER, "org-1")


def test_a_failed_enqueue_puts_the_incident_back(db, monkeypatch):
    db.pool(_row())
    import chat.background.task as task_mod

    def _broker_down(**kw):
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(task_mod.run_background_chat, "delay", _broker_down)
    with pytest.raises(RuntimeError):
        continue_investigation(INCIDENT, CLICKER, "org-1")
    assert any("aurora_status = 'complete'" in s and "rca_continuations - 1" in s for s in _sql(db))


@pytest.mark.parametrize("reason, words", [
    ("time_limit", "reached its time limit"),
    ("stalled", "stopped making progress"),
    ("error", "hit an error"),
    ("interrupted", "worker running it stopped"),
    (None, "was interrupted"),
])
def test_the_prompt_says_why_it_stopped(reason, words):
    prompt = continuation_prompt(reason)
    assert words in prompt
    assert "Do not repeat checks" in prompt


def test_a_long_report_is_capped_in_the_prompt():
    prompt = continuation_prompt("time_limit", "x" * 20000)
    assert "[report truncated]" in prompt
    assert len(prompt) < 9000


def test_is_continuation():
    assert is_continuation({"source": CONTINUE_SOURCE})
    assert not is_continuation({"source": "slack"})
    assert not is_continuation(None)


# --- Slack card ----------------------------------------------------------------------

def test_the_card_offers_to_continue_an_unfinished_rca():
    from utils.notifications.slack_notification_service import CONTINUE_ACTION_PREFIX, build_continue_blocks

    blocks = build_continue_blocks({"incident_id": INCIDENT, "unfinished_reason": "time_limit", "continuations": 0})
    button = blocks[0]["accessory"]
    assert button["action_id"] == CONTINUE_ACTION_PREFIX + INCIDENT
    assert button["value"] == INCIDENT
    assert "style" not in button  # neutral, not a warning
    assert "time limit" in blocks[0]["text"]["text"]


def test_the_card_has_no_button_once_the_cap_is_reached():
    from utils.notifications.slack_notification_service import build_continue_blocks

    blocks = build_continue_blocks(
        {"incident_id": INCIDENT, "unfinished_reason": "stalled", "continuations": MAX_CONTINUATIONS},
    )
    assert [b["type"] for b in blocks] == ["context"]


def test_a_finished_rca_card_is_unchanged():
    from utils.notifications.slack_notification_service import build_continue_blocks

    assert build_continue_blocks({"incident_id": INCIDENT, "unfinished_reason": None}) == []


# --- how runs end --------------------------------------------------------------------

@pytest.mark.parametrize("is_continued, expected", [(True, "complete"), (False, "error")])
def test_an_unsummarized_continuation_keeps_the_earlier_report(monkeypatch, is_continued, expected):
    import chat.background.task as task_mod

    statuses = []
    monkeypatch.setattr(
        task_mod, "_update_incident_aurora_status",
        lambda incident_id, status, user_id=None: statuses.append(status),
    )
    task_mod._end_unsummarized_run(INCIDENT, OWNER, is_continued)
    assert statuses == [expected]


@pytest.fixture
def dead_rca(monkeypatch):
    import chat.background.task as task_mod
    import utils.notifications.dispatcher as dispatcher

    calls = SimpleNamespace(summarized=[], failed=[], summarize_ok=True, findings=[])
    monkeypatch.setattr(
        task_mod, "_mark_inflight_findings_failed",
        lambda incident_id, user_id, reason: calls.findings.append(incident_id) or 0,
    )

    def _summarize(incident_id, user_id, session_id, reason):
        calls.summarized.append((incident_id, session_id, reason))
        return calls.summarize_ok

    monkeypatch.setattr(task_mod, "_summarize_unfinished_investigation", _summarize)
    monkeypatch.setattr(
        dispatcher, "notify_investigation_failed",
        lambda user_id, incident_id, error_message=None: calls.failed.append(error_message),
    )
    return task_mod, calls


def test_a_dead_workers_rca_with_evidence_becomes_a_report(dead_rca):
    task_mod, calls = dead_rca
    task_mod._report_or_fail_dead_rca(OWNER, INCIDENT, SESSION, "worker died")
    assert calls.summarized == [(INCIDENT, SESSION, "interrupted")]
    assert calls.failed == []
    # Sub-agent rows stop spinning before the incident leaves 'error'.
    assert calls.findings == [INCIDENT]


def test_a_dead_worker_with_nothing_to_report_still_fails(dead_rca):
    task_mod, calls = dead_rca
    calls.summarize_ok = False
    task_mod._report_or_fail_dead_rca(OWNER, INCIDENT, SESSION, "worker died")
    assert calls.failed == ["worker died"]


def test_a_dead_follow_up_chat_is_not_summarized(dead_rca):
    task_mod, calls = dead_rca
    task_mod._report_or_fail_dead_rca(OWNER, INCIDENT, None, "worker died")
    assert calls.summarized == []
    assert calls.failed == ["worker died"]


@pytest.mark.parametrize("reason", ["error", "interrupted"])
def test_the_summary_knows_every_reason(reason):
    from chat.background.summarization import UNFINISHED_REASONS, _unfinished_investigation_note

    assert reason in UNFINISHED_REASONS
    assert UNFINISHED_REASONS[reason] in _unfinished_investigation_note(reason)


# --- the continuation's history ------------------------------------------------------

def test_history_is_rebuilt_from_execution_steps(monkeypatch):
    from langchain_core.messages import HumanMessage

    cursor = MagicMock()
    cursor.fetchall.return_value = [
        ("cloud_exec", {"command": "kubectl get pods"}, "pod-a CrashLoopBackOff"),
        ("dispatch_subagent", {}, "rollup"),  # coordination, not evidence
        ("query_datadog", '{"query": "errors"}', "x" * 5000),
        ("cloud_exec", {}, "  "),  # no output
    ]
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    pool = MagicMock()
    pool.get_admin_connection.return_value.__enter__.return_value = conn
    monkeypatch.setattr(continuation, "db_pool", pool)
    monkeypatch.setattr(continuation, "set_rls_context", lambda *a, **k: "org-1")

    history = continuation.history_from_execution_steps(INCIDENT, OWNER)

    # One plain message: replayed tool-call messages are rejected by some providers.
    assert len(history) == 1 and isinstance(history[0], HumanMessage)
    text = history[0].content
    assert "2 tool calls" in text
    assert "### cloud_exec" in text and "kubectl get pods" in text and "CrashLoopBackOff" in text
    assert "### query_datadog" in text and "[truncated]" in text
    assert "dispatch_subagent" not in text


def test_no_history_when_steps_cannot_be_read(monkeypatch):
    pool = MagicMock()
    pool.get_admin_connection.side_effect = RuntimeError("db down")
    monkeypatch.setattr(continuation, "db_pool", pool)
    assert continuation.history_from_execution_steps(INCIDENT, OWNER) == []


def test_session_and_recorded_citations_are_merged():
    from chat.background.citation_extractor import Citation, CitationExtractor

    def c(i, call, out):
        return Citation(index=i, tool_name="t", command="c", output=out, timestamp=None, tool_call_id=call)

    steps = [c(1, "a", "step a (truncated)"), c(2, "b", "step b")]
    session = [c(1, "b", "full b"), c(2, "z", "new z")]
    merged = CitationExtractor.merge_citations(session, steps)
    assert [(m.index, m.tool_call_id, m.output) for m in merged] == [
        (1, "a", "step a (truncated)"), (2, "b", "full b"), (3, "z", "new z"),
    ]
