"""A healthy investigation must not be reported as a dead worker.

The cleanup sweep called a running investigation dead after three minutes with
no saved thought, no stream event and no tool call marked running. All three
stand still while the model works through one long step. The worker now beats
the session every 30 seconds on its own thread (``heartbeat_at``), and the sweep
trusts a recent beat.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from chat.background import liveness

SESSION = "22222222-2222-2222-2222-222222222222"
USER = "user-1"


def _wait_for(condition, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return False


# --- the heartbeat -------------------------------------------------------------

@pytest.fixture
def beats(monkeypatch):
    """Record every beat instead of touching a database."""
    seen = []
    monkeypatch.setattr(liveness, "touch_session", lambda session_id, user_id: seen.append((session_id, user_id)) or True)
    return seen


def test_first_beat_is_immediate_and_beats_continue_until_the_block_ends(beats):
    with liveness.session_heartbeat(SESSION, USER, interval=0.01):
        assert _wait_for(lambda: len(beats) >= 3)
    assert beats[0] == (SESSION, USER)
    count_at_exit = len(beats)
    time.sleep(0.05)
    assert len(beats) == count_at_exit, "the heartbeat outlived its task"


def test_a_long_interval_still_beats_once_at_the_start(beats):
    # A task that waited in the queue starts with a session that already looks stale.
    with liveness.session_heartbeat(SESSION, USER, interval=60):
        assert _wait_for(lambda: len(beats) == 1)
    assert len(beats) == 1


def test_beats_come_from_another_thread_while_the_event_loop_is_blocked(beats):
    """The reason this is a thread and not an asyncio task."""
    main_thread = threading.get_ident()
    threads = []
    original = liveness.touch_session

    def _record(session_id, user_id):
        threads.append(threading.get_ident())
        return original(session_id, user_id)

    liveness.touch_session = _record
    try:
        async def _blocks_the_loop():
            before = len(beats)
            time.sleep(0.15)  # a synchronous call on the loop thread
            return len(beats) - before

        with liveness.session_heartbeat(SESSION, USER, interval=0.01):
            during_block = asyncio.run(_blocks_the_loop())
    finally:
        liveness.touch_session = original

    assert during_block >= 3
    assert threads and main_thread not in threads


def test_a_failed_beat_is_logged_and_the_next_one_still_runs(monkeypatch):
    calls = []

    def _flaky(session_id, user_id):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database restarting")
        return True

    monkeypatch.setattr(liveness, "touch_session", _flaky)
    with liveness.session_heartbeat(SESSION, USER, interval=0.01):
        assert _wait_for(lambda: len(calls) >= 3)


def test_an_error_in_the_task_stops_the_heartbeat_and_propagates(beats):
    def _workflow_fails():
        with liveness.session_heartbeat(SESSION, USER, interval=0.01):
            assert _wait_for(lambda: len(beats) >= 1)
            raise ValueError("workflow failed")

    with pytest.raises(ValueError):
        _workflow_fails()
    count_at_exit = len(beats)
    time.sleep(0.05)
    assert len(beats) == count_at_exit


@pytest.mark.parametrize("session_id, user_id", [(None, USER), (SESSION, None), ("", "")])
def test_no_heartbeat_without_a_session_and_a_user(beats, session_id, user_id):
    before = threading.active_count()
    with liveness.session_heartbeat(session_id, user_id, interval=0.01):
        time.sleep(0.03)
        assert threading.active_count() == before
    assert beats == []


class _FakePool:
    def __init__(self, rowcount=1):
        self.cursor = MagicMock()
        self.cursor.rowcount = rowcount
        self.conn = MagicMock()
        self.conn.cursor.return_value.__enter__.return_value = self.cursor
        self.pool = MagicMock()
        self.pool.get_admin_connection.return_value.__enter__.return_value = self.conn


def _wire_db(monkeypatch, pool, org_id="org-1"):
    import utils.auth.stateless_auth as stateless_auth
    import utils.db.connection_pool as connection_pool

    monkeypatch.setattr(connection_pool, "db_pool", pool.pool)
    rls = MagicMock(return_value=org_id)
    monkeypatch.setattr(stateless_auth, "set_rls_context", rls)
    return rls


def test_touch_only_refreshes_a_session_that_is_still_in_progress(monkeypatch):
    pool = _FakePool(rowcount=1)
    rls = _wire_db(monkeypatch, pool)

    assert liveness.touch_session(SESSION, USER) is True

    rls.assert_called_once()
    assert rls.call_args.args[2] == USER
    sql, params = pool.cursor.execute.call_args.args
    assert " ".join(sql.split()) == (
        "UPDATE chat_sessions SET heartbeat_at = NOW() WHERE id = %s AND status = 'in_progress'"
    )
    assert params == (SESSION,)
    pool.conn.commit.assert_called_once()


def test_touch_reports_a_finished_session_as_not_touched(monkeypatch):
    pool = _FakePool(rowcount=0)
    _wire_db(monkeypatch, pool)
    assert liveness.touch_session(SESSION, USER) is False


def test_touch_does_nothing_when_the_org_cannot_be_resolved(monkeypatch):
    pool = _FakePool()
    _wire_db(monkeypatch, pool, org_id=None)
    assert liveness.touch_session(SESSION, USER) is False
    pool.cursor.execute.assert_not_called()


# --- the sweep's decision ---------------------------------------------------------

@pytest.fixture
def task(monkeypatch):
    """Import chat.background.task with Redis stubbed (it builds a client at import)."""
    import utils.cache.redis_client as redis_client

    monkeypatch.setattr(redis_client, "get_redis_client", MagicMock)
    monkeypatch.delitem(sys.modules, "chat.background.task", raising=False)
    import chat.background.task as task_mod

    monkeypatch.delitem(sys.modules, "chat.background.task", raising=False)
    return task_mod


NOW = datetime(2026, 11, 10, 17, 46, 11)
THRESHOLD = NOW - timedelta(minutes=3)
FRESH_BEAT = NOW - timedelta(seconds=20)
STALE_BEAT = NOW - timedelta(minutes=4, seconds=26)


def _celery_says(monkeypatch, task, state, result=None):
    monkeypatch.setattr(task.celery_app, "AsyncResult", lambda _task_id: SimpleNamespace(state=state, result=result))


def _cursor(*rows):
    cursor = MagicMock()
    cursor.fetchone.side_effect = list(rows)
    return cursor


def test_quiet_model_step_with_a_fresh_beat_is_alive(monkeypatch, task):
    """One model step of several minutes: no thought saved, no tool running. The
    heartbeat is the only thing that moved, and it is enough."""
    _celery_says(monkeypatch, task, "STARTED")
    cursor = _cursor()

    assert task._is_task_dead("task-1", FRESH_BEAT, THRESHOLD, cursor=cursor, incident_id="inc-1") is False
    cursor.execute.assert_not_called()  # a fresh beat needs no second opinion


def test_started_task_with_no_beat_and_nothing_running_is_dead(monkeypatch, task):
    """A worker that is really gone. Before the heartbeat, a quiet model step
    looked exactly like this, which is where the false notices came from."""
    _celery_says(monkeypatch, task, "STARTED")
    cursor = _cursor(None, None)  # no running tool call, no running sub-agent

    assert task._is_task_dead("task-1", STALE_BEAT, THRESHOLD, cursor=cursor, incident_id="inc-1") is True


@pytest.mark.parametrize("worker_beats, expected_dead", [(True, False), (False, True)],
                         ids=["with the heartbeat", "without it"])
def test_sweep_verdict_after_a_quiet_model_step(monkeypatch, task, worker_beats, expected_dead):
    """The whole failure in miniature. A model step runs longer than the sweep's
    window while nothing else moves; only the session timestamp tells the sweep
    whether the worker is there. Time is scaled down: window 0.1 s, step 0.3 s."""
    session = {"updated_at": datetime.now()}  # last touched when the task started
    monkeypatch.setattr(
        liveness, "touch_session",
        lambda _session_id, _user_id: session.update(updated_at=datetime.now()) or True,
    )
    _celery_says(monkeypatch, task, "STARTED")

    def _quiet_model_step():
        time.sleep(0.3)

    if worker_beats:
        with liveness.session_heartbeat(SESSION, USER, interval=0.02):
            _quiet_model_step()
            verdict = task._is_task_dead(
                "task-1", session["updated_at"], datetime.now() - timedelta(seconds=0.1),
                cursor=_cursor(None, None), incident_id="inc-1",
            )
    else:
        _quiet_model_step()
        verdict = task._is_task_dead(
            "task-1", session["updated_at"], datetime.now() - timedelta(seconds=0.1),
            cursor=_cursor(None, None), incident_id="inc-1",
        )

    assert verdict is expected_dead


@pytest.mark.parametrize("rows", [((1,),), (None, (1,))], ids=["running tool call", "running sub-agent"])
def test_older_worker_without_beats_is_still_alive_while_something_runs(monkeypatch, task, rows):
    _celery_says(monkeypatch, task, "STARTED")
    cursor = _cursor(*rows)

    assert task._is_task_dead("task-1", STALE_BEAT, THRESHOLD, cursor=cursor, incident_id="inc-1") is False


def test_task_that_outlived_result_expires_is_alive_while_it_beats(monkeypatch, task):
    """After result_expires the stored state reads PENDING with no result,
    which used to mean dead whatever the worker was doing."""
    _celery_says(monkeypatch, task, "PENDING", result=None)

    assert task._is_task_dead("task-1", FRESH_BEAT, THRESHOLD) is False


def test_pending_task_with_no_beat_is_dead(monkeypatch, task):
    _celery_says(monkeypatch, task, "PENDING", result=None)

    assert task._is_task_dead("task-1", STALE_BEAT, THRESHOLD) is True


@pytest.mark.parametrize("state", ["FAILURE", "REVOKED"])
def test_failed_or_revoked_task_is_dead_even_with_a_fresh_beat(monkeypatch, task, state):
    _celery_says(monkeypatch, task, state)

    assert task._is_task_dead("task-1", FRESH_BEAT, THRESHOLD) is True


def test_finished_task_is_not_dead(monkeypatch, task):
    _celery_says(monkeypatch, task, "SUCCESS", result={"status": "completed"})

    assert task._is_task_dead("task-1", STALE_BEAT, THRESHOLD) is False


def _run_watched_calls(path: Path, function: str) -> list:
    """Calls to run_watched inside *function*, read with ast so formatting does not matter.

    Read from the file: where Celery is stubbed the task decorator hides the function.
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    func = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == function)
    return [
        n for n in ast.walk(func)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "run_watched"
    ]


@pytest.mark.parametrize("module, function", [
    ("chat/background/task.py", "run_background_chat"),
    ("chat/background/prediscovery_task.py", "run_prediscovery"),
])
def test_every_background_run_goes_through_the_heartbeat_and_watchdog(module, function):
    """Both callers of _execute_background_chat run it under run_watched, with the
    session it vouches for. A run outside it never beats, and the sweep would fail
    it as a dead worker after five minutes."""
    server = Path(__file__).resolve().parents[2]
    calls = _run_watched_calls(server / module, function)
    assert len(calls) == 1
    workflow, session_arg, user_arg = calls[0].args
    assert getattr(workflow.func, "id", None) == "_execute_background_chat"
    assert (session_arg.id, user_arg.id) == ("session_id", "user_id")
