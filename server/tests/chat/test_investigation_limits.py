"""How an investigation ends: slow thinking is not a stall, and limits end in a report.

* A model can think for many minutes before its first token, which emits no
  event. An open model call is a bounded call, so the stall watchdog waits for
  it up to its own limit instead of cancelling a healthy run.
* Shortly before the time limit the agent is told once to stop and conclude.
* When the time limit or the stall watchdog still stops a primary RCA, the
  evidence it gathered is summarized as a partial report instead of thrown away.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from chat.background import liveness
from utils import progress_watch
from utils.progress_watch import ModelCallBounds, ProgressTracker

SESSION = "44444444-4444-4444-4444-444444444444"
USER = "user-1"
INCIDENT = "55555555-5555-5555-5555-555555555555"


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def watched():
    """A tracker installed as the current one, with a controllable clock."""
    clock = _Clock()
    tracker = ProgressTracker(600, clock=clock, wrap_up_after=3000)
    token = progress_watch.set_tracker(tracker)
    yield tracker, clock
    progress_watch.reset_tracker(token)


# --- model calls -------------------------------------------------------------------

def test_a_model_thinking_past_the_idle_window_is_not_a_stall(watched):
    tracker, clock = watched
    bounds = ModelCallBounds(1800)
    bounds.start("run-1")
    clock.now += 1500  # 25 minutes of thinking, no token yet
    assert tracker.stall_reason() is None


def test_a_model_call_past_its_own_limit_is_a_stall(watched):
    tracker, clock = watched
    bounds = ModelCallBounds(1800)
    bounds.start("run-1")
    clock.now += 1800 + progress_watch.BOUND_GRACE_SECONDS + 1
    assert tracker.stall_reason() == "model call ran 1921s, past its 1800s limit"


def test_the_end_of_a_model_call_restarts_the_idle_window(watched):
    tracker, clock = watched
    bounds = ModelCallBounds(1800)
    bounds.start("run-1")
    clock.now += 1500
    bounds.end("run-1")
    clock.now += 599
    assert tracker.stall_reason() is None
    clock.now += 2
    assert tracker.stall_reason() == "no progress for 601s"


def test_ending_a_call_twice_or_an_unknown_one_is_harmless(watched):
    tracker, clock = watched
    bounds = ModelCallBounds(1800)
    bounds.start("run-1")
    bounds.end("run-1")
    bounds.end("run-1")
    bounds.end("never-started")
    bounds.end(None)
    clock.now += 601
    assert tracker.stall_reason() == "no progress for 601s"


def test_close_all_releases_calls_left_open_by_an_error(watched):
    tracker, clock = watched
    bounds = ModelCallBounds(1800)
    bounds.start("run-1")
    bounds.start("run-2")
    bounds.close_all()
    clock.now += 601
    assert tracker.stall_reason() == "no progress for 601s"


def test_a_zero_model_limit_counts_thinking_against_the_idle_window(watched):
    tracker, clock = watched
    bounds = ModelCallBounds(0)
    bounds.start("run-1")
    clock.now += 601
    assert tracker.stall_reason() == "no progress for 601s"


def test_model_bounds_do_nothing_outside_a_watched_run():
    bounds = ModelCallBounds(1800)
    bounds.start("run-1")
    bounds.end("run-1")
    bounds.close_all()


def test_a_slow_model_call_is_waited_for_end_to_end(monkeypatch):
    """Through the real watchdog: quiet far past the idle window, still not cancelled."""
    monkeypatch.setattr(liveness, "touch_session", lambda s, u: True)

    async def _thinks_then_answers():
        bounds = ModelCallBounds(60)
        bounds.start("run-1")
        await asyncio.sleep(0.4)  # four idle windows with no event at all
        bounds.end("run-1")
        return "answer"

    result = liveness.run_watched(_thinks_then_answers(), SESSION, USER, interval=0.01, idle_seconds=0.1)
    assert result == "answer"


def test_a_hung_model_call_is_still_cancelled_end_to_end(monkeypatch):
    monkeypatch.setattr(liveness, "touch_session", lambda s, u: True)
    monkeypatch.setattr(progress_watch, "BOUND_GRACE_SECONDS", 0)

    async def _never_answers():
        bounds = ModelCallBounds(0.1)
        bounds.start("run-1")
        await asyncio.Event().wait()

    started = time.monotonic()
    with pytest.raises(liveness.InvestigationStalled, match="model call ran"):
        liveness.run_watched(_never_answers(), SESSION, USER, interval=0.01, idle_seconds=0.1)
    assert time.monotonic() - started < 2


# --- wrap-up -----------------------------------------------------------------------

def test_wrap_up_is_claimed_once_per_session_after_its_time(watched):
    tracker, clock = watched
    assert not progress_watch.claim_wrap_up(SESSION)
    clock.now += 3000
    assert progress_watch.claim_wrap_up(SESSION)
    assert not progress_watch.claim_wrap_up(SESSION)
    # A sub-agent session is told on its own.
    assert progress_watch.claim_wrap_up(SESSION + "::sa_1")


def test_no_wrap_up_without_a_wrap_up_time():
    clock = _Clock()
    tracker = ProgressTracker(600, clock=clock)
    clock.now += 100_000
    assert not tracker.claim_wrap_up(SESSION)
    assert not progress_watch.claim_wrap_up(SESSION)  # no tracker installed at all


def _state(**over):
    values = {"is_background": True, "session_id": SESSION, "messages": []}
    values.update(over)
    return SimpleNamespace(**values)


def test_the_agent_is_told_once_to_conclude(watched):
    from chat.background.context_updates import WRAP_UP_NOTICE, apply_wrap_up_notice

    _, clock = watched
    state = _state()
    assert apply_wrap_up_notice(state) is None  # too early
    clock.now += 3000
    message = apply_wrap_up_notice(state)
    assert message is not None and message.content == WRAP_UP_NOTICE
    assert state.messages == [message]  # later model calls keep seeing it
    assert apply_wrap_up_notice(state) is None


def test_interactive_chats_are_never_told_to_conclude(watched):
    from chat.background.context_updates import apply_wrap_up_notice

    _, clock = watched
    clock.now += 3000
    assert apply_wrap_up_notice(_state(is_background=False)) is None
    assert apply_wrap_up_notice(None) is None


def test_the_notice_forbids_new_tools_and_guessing():
    from chat.background.context_updates import WRAP_UP_NOTICE

    assert "Do not start any new tool calls" in WRAP_UP_NOTICE
    assert "what you could not check" in WRAP_UP_NOTICE
    assert "instead of guessing" in WRAP_UP_NOTICE


# --- settings ----------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("", 1800), ("2400", 2400), ("0", 0), ("-1", 0), ("30", 120), ("long", 1800),
])
def test_model_call_setting(monkeypatch, raw, expected):
    from chat.investigation_timeout import investigation_model_call_seconds
    monkeypatch.setenv("INVESTIGATION_MODEL_CALL_SECONDS", raw)
    assert investigation_model_call_seconds() == expected


@pytest.mark.parametrize("timeout, wrap_up, expected", [
    ("3600", "", 3600 - 60 - 300),   # defaults: five minutes before the soft limit
    ("7200", "600", 7200 - 60 - 600),
    ("3600", "0", None),             # turned off
    ("3600", "10", 3600 - 60 - 60),  # below the minimum
    ("300", "300", None),            # not even a minute left to investigate
])
def test_wrap_up_time_setting(monkeypatch, timeout, wrap_up, expected):
    from chat.investigation_timeout import investigation_wrap_up_after_seconds
    monkeypatch.setenv("INVESTIGATION_TIMEOUT_SECONDS", timeout)
    monkeypatch.setenv("INVESTIGATION_WRAPUP_SECONDS", wrap_up)
    assert investigation_wrap_up_after_seconds() == expected


def test_the_stall_setting_is_unchanged(monkeypatch):
    from chat.investigation_timeout import investigation_stall_seconds
    for raw, expected in [("", 600), ("900", 900), ("0", 0), ("30", 120), ("x", 600)]:
        monkeypatch.setenv("INVESTIGATION_STALL_SECONDS", raw)
        assert investigation_stall_seconds() == expected


def test_run_watched_installs_the_wrap_up_time(monkeypatch):
    monkeypatch.setattr(liveness, "touch_session", lambda s, u: True)
    monkeypatch.setenv("INVESTIGATION_TIMEOUT_SECONDS", "3600")
    monkeypatch.delenv("INVESTIGATION_WRAPUP_SECONDS", raising=False)

    async def _peek():
        tracker = progress_watch.current_tracker()
        return tracker._wrap_up_at - tracker._last_progress

    after = liveness.run_watched(_peek(), SESSION, USER, interval=0.01, idle_seconds=600)
    assert after == pytest.approx(3600 - 60 - 300, abs=1)


# --- the partial report ------------------------------------------------------------

@pytest.mark.parametrize("reason, words", [
    ("time_limit", "reached its time limit"),
    ("stalled", "stopped making progress"),
])
def test_the_summary_is_told_the_investigation_is_incomplete(reason, words):
    from chat.background.summarization import _unfinished_investigation_note

    note = _unfinished_investigation_note(reason)
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
