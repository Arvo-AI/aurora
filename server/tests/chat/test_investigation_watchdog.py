"""An investigation that stops making progress is stopped, not left to the time limit.

The heartbeat proves the worker process is alive, and keeps proving it while the
investigation waits on a call that will never return. The watchdog is what ends
that wait: it cancels a run with no progress past its deadline, and a call that
enforces its own limit (a command, a sub-agent) pushes the deadline out to that
limit, so a long query is waited for and a hang is not.
"""

from __future__ import annotations

import asyncio
import signal
import sys
import threading
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from chat.background import liveness
from utils import progress_watch
from utils.progress_watch import ProgressTracker, bounded_call, note_progress

SESSION = "33333333-3333-3333-3333-333333333333"
USER = "user-1"


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


# --- the tracker ------------------------------------------------------------------

def test_quiet_past_the_idle_window_is_a_stall():
    clock = _Clock()
    tracker = ProgressTracker(600, clock=clock)
    clock.now += 599
    assert tracker.stall_reason() is None
    clock.now += 2
    assert tracker.stall_reason() == "no progress for 601s"


def test_progress_restarts_the_idle_window():
    clock = _Clock()
    tracker = ProgressTracker(600, clock=clock)
    clock.now += 500
    tracker.progress()
    clock.now += 500
    assert tracker.stall_reason() is None


def test_a_long_command_inside_its_own_timeout_is_not_a_stall():
    """A 20-minute query with a 20-minute timeout runs well past the idle window."""
    clock = _Clock()
    tracker = ProgressTracker(600, clock=clock)
    with tracker.bounded(1200, "command 'bq'"):
        clock.now += 1200 + progress_watch.BOUND_GRACE_SECONDS
        assert tracker.stall_reason() is None
        clock.now += 1
        assert tracker.stall_reason() == "command 'bq' ran 1321s, past its 1200s limit"


def test_the_end_of_a_bounded_call_counts_as_progress():
    clock = _Clock()
    tracker = ProgressTracker(600, clock=clock)
    with tracker.bounded(1200, "command 'bq'"):
        clock.now += 1000
    clock.now += 599
    assert tracker.stall_reason() is None


def test_parallel_sub_agents_are_waited_for_until_the_longest_limit():
    clock = _Clock()
    tracker = ProgressTracker(600, clock=clock)
    with tracker.bounded(300, "sub-agent a"), tracker.bounded(900, "sub-agent b"):
        clock.now += 900 + progress_watch.BOUND_GRACE_SECONDS
        assert tracker.stall_reason() is None
        clock.now += 1
        assert "sub-agent b" in tracker.stall_reason()


def test_a_zero_idle_window_turns_the_check_off():
    clock = _Clock()
    tracker = ProgressTracker(0, clock=clock)
    clock.now += 10_000
    assert tracker.stall_reason() is None


def test_reports_outside_a_watched_run_do_nothing():
    note_progress()
    with bounded_call(10, "command 'kubectl'"):
        pass


# --- the watchdog, end to end ------------------------------------------------------

@pytest.fixture
def beats(monkeypatch):
    seen = []
    monkeypatch.setattr(liveness, "touch_session", lambda s, u: seen.append(time.monotonic()) or True)
    return seen


def test_a_hung_call_is_cancelled_and_reported_as_a_stall(beats):
    async def _waits_forever():
        await asyncio.Event().wait()  # a reply that never comes

    started = time.monotonic()
    with pytest.raises(liveness.InvestigationStalled, match="no progress"):
        liveness.run_watched(_waits_forever(), SESSION, USER, interval=0.01, idle_seconds=0.1)
    assert time.monotonic() - started < 2


def test_a_run_that_keeps_reporting_progress_is_left_alone(beats):
    async def _streams():
        for _ in range(30):
            note_progress()  # a model chunk
            await asyncio.sleep(0.01)
        return "answer"

    assert liveness.run_watched(_streams(), SESSION, USER, interval=0.01, idle_seconds=0.1) == "answer"


def test_a_long_command_inside_its_limit_is_left_alone(beats):
    async def _long_command():
        with bounded_call(1.0, "command 'bq'"):
            await asyncio.sleep(0.4)  # four idle windows, inside its own limit
        return "rows"

    assert liveness.run_watched(_long_command(), SESSION, USER, interval=0.01, idle_seconds=0.1) == "rows"


def test_a_command_past_its_own_limit_is_a_stall(beats, monkeypatch):
    monkeypatch.setattr(progress_watch, "BOUND_GRACE_SECONDS", 0)

    async def _overruns():
        with bounded_call(0.1, "command 'kubectl'"):
            await asyncio.Event().wait()

    with pytest.raises(liveness.InvestigationStalled, match="command 'kubectl'"):
        liveness.run_watched(_overruns(), SESSION, USER, interval=0.01, idle_seconds=0.05)


def test_progress_from_a_tool_thread_reaches_the_watchdog(beats):
    """Sync tools run on executor threads that copy the context, as LangChain does."""
    def _tool():
        for _ in range(30):
            note_progress()
            time.sleep(0.01)
        return "done"

    async def _runs_tool():
        return await asyncio.to_thread(_tool)

    assert liveness.run_watched(_runs_tool(), SESSION, USER, interval=0.01, idle_seconds=0.1) == "done"


def test_a_blocked_loop_stops_the_beats_and_asks_for_an_interrupt(beats, monkeypatch):
    """The cancel cannot land while a synchronous call blocks the loop. The
    watchdog then stops vouching for the worker, so the sweep fails it, and
    asks Celery's soft-limit handler to break the call."""
    monkeypatch.setattr(liveness, "CANCEL_GRACE_SECONDS", 0.1)
    interrupts = []
    monkeypatch.setattr(liveness, "_interrupt_main_thread", lambda: interrupts.append(time.monotonic()) or True)

    async def _blocks_then_awaits():
        time.sleep(0.6)  # a synchronous call with no timeout
        await asyncio.sleep(0)

    with pytest.raises(liveness.InvestigationStalled):
        liveness.run_watched(_blocks_then_awaits(), SESSION, USER, interval=0.01, idle_seconds=0.1)

    assert interrupts, "the watchdog never escalated"
    beats_after_giving_up = [b for b in beats if b > interrupts[0]]
    assert beats_after_giving_up == []


def test_the_soft_limit_raised_by_the_watchdog_is_reported_as_a_stall(beats, monkeypatch):
    monkeypatch.setattr(liveness, "CANCEL_GRACE_SECONDS", 0.05)

    class _SoftTimeLimitExceeded(Exception):
        pass

    def _interrupt():
        raise_in_main.set()
        return True

    raise_in_main = threading.Event()
    monkeypatch.setattr(liveness, "_interrupt_main_thread", _interrupt)

    async def _blocked():
        # Stands in for the blocked call the signal handler breaks out of.
        while not raise_in_main.wait(0.01):
            pass
        raise _SoftTimeLimitExceeded()

    with pytest.raises(liveness.InvestigationStalled) as info:
        liveness.run_watched(_blocked(), SESSION, USER, interval=0.01, idle_seconds=0.05)
    assert isinstance(info.value.__cause__, _SoftTimeLimitExceeded)


def test_an_ordinary_error_is_not_dressed_up_as_a_stall(beats):
    async def _fails():
        raise ValueError("provider rejected the request")

    with pytest.raises(ValueError):
        liveness.run_watched(_fails(), SESSION, USER, interval=0.01, idle_seconds=60)


@pytest.mark.skipif(not hasattr(signal, "SIGUSR1"), reason="POSIX only")
def test_no_interrupt_without_a_soft_limit_handler(monkeypatch):
    """SIGUSR1's default action kills the process."""
    monkeypatch.setattr(signal, "getsignal", lambda _sig: signal.SIG_DFL)
    kills = []
    monkeypatch.setattr(liveness.os, "kill", lambda *a: kills.append(a))
    assert liveness._interrupt_main_thread() is False
    assert kills == []


@pytest.mark.skipif(not hasattr(signal, "SIGUSR1"), reason="POSIX only")
def test_interrupt_uses_the_installed_soft_limit_handler(monkeypatch):
    monkeypatch.setattr(signal, "getsignal", lambda _sig: lambda *_a: None)
    kills = []
    monkeypatch.setattr(liveness.os, "kill", lambda *a: kills.append(a))
    assert liveness._interrupt_main_thread() is True
    assert kills == [(liveness.os.getpid(), signal.SIGUSR1)]


# --- the timings line up -------------------------------------------------------------

@pytest.fixture
def task():
    import chat.background.task as task_mod
    return task_mod


def test_beats_are_far_more_frequent_than_the_sweep_windows(task):
    """The original bug: a refresh every 5 minutes against a 3-minute window."""
    assert liveness.HEARTBEAT_INTERVAL_SECONDS * 5 <= 3 * 60  # dead-task window
    assert liveness.HEARTBEAT_INTERVAL_SECONDS * 10 <= task._DEAD_WORKER_MINUTES * 60


def test_the_stall_window_outlasts_a_model_call_with_its_retries():
    from chat.investigation_timeout import DEFAULT_INVESTIGATION_STALL_SECONDS
    # The slowest provider setting: 120 s per attempt, 3 retries.
    assert DEFAULT_INVESTIGATION_STALL_SECONDS > 120 * 4


@pytest.mark.parametrize("raw, expected", [
    ("", 600), ("900", 900), ("0", 0), ("-5", 0), ("30", 120), ("soon", 600),
])
def test_stall_window_setting(monkeypatch, raw, expected):
    from chat.investigation_timeout import investigation_stall_seconds
    monkeypatch.setenv("INVESTIGATION_STALL_SECONDS", raw)
    assert investigation_stall_seconds() == expected


# --- the sweep -----------------------------------------------------------------------

DB_NOW = datetime(2026, 11, 11, 12, 0, 0)


class _ScriptedCursor:
    """Answers the sweep's queries from a script keyed on a fragment of the SQL."""

    def __init__(self, script):
        self.script = script
        self.executed = []
        self._last = None
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))
        self._last = " ".join(sql.split())

    def _answer(self):
        for fragment, rows in self.script.items():
            if fragment in self._last:
                return rows
        return []

    def fetchone(self):
        rows = self._answer()
        return rows[0] if rows else None

    def fetchall(self):
        return list(self._answer())

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _run_sweep(monkeypatch, task, script):
    cursor = _ScriptedCursor({"SELECT LOCALTIMESTAMP": [(DB_NOW,)], "FROM users": [("u1",)], **script})
    conn = MagicMock()
    conn.cursor.return_value = cursor
    pool = MagicMock()
    pool.get_admin_connection.return_value.__enter__.return_value = conn
    monkeypatch.setattr(task, "db_pool", pool)
    monkeypatch.setattr(task, "set_rls_context", lambda *a, **k: "org-1")
    monkeypatch.setattr(task, "_propagate_suggestion_status", lambda *a, **k: None)
    monkeypatch.setattr(task, "_record_rca_error", lambda *a, **k: None)
    monkeypatch.setattr(task, "_notify_orphaned_chat_reply", lambda *a, **k: None)
    revoked = []
    monkeypatch.setattr(task.celery_app, "control", SimpleNamespace(
        revoke=lambda task_id, terminate=False: revoked.append((task_id, terminate)),
    ))
    import utils.notifications.dispatcher as dispatcher
    monkeypatch.setattr(dispatcher, "notify_investigation_failed", lambda *a, **k: None)
    task.cleanup_stale_background_chats()
    return cursor, revoked


def test_a_beaten_session_is_failed_after_five_quiet_minutes_and_a_queued_one_after_twenty(monkeypatch, task):
    cursor, _ = _run_sweep(monkeypatch, task, {})
    sql, params = next(e for e in cursor.executed if "cs.heartbeat_at IS NOT NULL" in e[0])
    assert "GREATEST(cs.heartbeat_at, cs.updated_at) < %s" in sql
    assert "cs.heartbeat_at IS NULL AND cs.updated_at < %s" in sql
    # Thresholds come from the database clock that wrote the beats.
    assert params == ("u1", DB_NOW - timedelta(minutes=5), DB_NOW - timedelta(minutes=20))


def test_the_dead_task_check_reads_the_heartbeat(monkeypatch, task):
    cursor, _ = _run_sweep(monkeypatch, task, {})
    sql, params = next(e for e in cursor.executed if "i.rca_celery_task_id IS NOT NULL" in e[0])
    assert "GREATEST(cs.heartbeat_at, COALESCE(cs.updated_at, i.updated_at)) as last_activity" in sql
    assert params == (DB_NOW - timedelta(minutes=3), "u1")


def test_a_dead_task_is_revoked_so_a_stuck_worker_frees_its_slot(monkeypatch, task):
    stale = DB_NOW - timedelta(minutes=6)
    monkeypatch.setattr(task.celery_app, "AsyncResult", lambda _t: SimpleNamespace(state="STARTED", result=None))
    _, revoked = _run_sweep(monkeypatch, task, {
        "i.rca_celery_task_id IS NOT NULL": [("inc-1", "task-1", SESSION, stale)],
        "FROM execution_steps": [],
        "FROM rca_findings": [],
        "UPDATE incidents SET aurora_status = 'error'": [("inc-1",)],
        "UPDATE chat_sessions": [(SESSION, {})],
    })
    assert revoked == [("task-1", True)]


def test_a_live_task_is_not_revoked(monkeypatch, task):
    fresh = DB_NOW - timedelta(seconds=20)
    monkeypatch.setattr(task.celery_app, "AsyncResult", lambda _t: SimpleNamespace(state="STARTED", result=None))
    _, revoked = _run_sweep(monkeypatch, task, {
        "i.rca_celery_task_id IS NOT NULL": [("inc-1", "task-1", SESSION, fresh)],
    })
    assert revoked == []
