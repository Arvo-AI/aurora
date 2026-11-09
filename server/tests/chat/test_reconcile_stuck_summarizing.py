"""Tests for the periodic re-drive of incidents stranded in 'summarizing'.

A hard time-limit SIGKILL of the summary task's fork persists nothing — no
summary, no status, no retry — so the incident stays 'summarizing' forever and
no other sweeper covers that state (cleanup_stale_background_chats only handles
'running'). These target ``reconcile_stuck_summarizing``; conftest stubs
``celery`` and the DB connection pool, and nothing here dials Postgres.
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from chat.background import task as mod

USER_A = "11111111-1111-1111-1111-11111111111a"
USER_B = "11111111-1111-1111-1111-11111111111b"
INC_1 = "22222222-2222-2222-2222-222222222221"
INC_2 = "22222222-2222-2222-2222-222222222222"
SESSION_1 = "33333333-3333-3333-3333-333333333331"
SESSION_2 = "33333333-3333-3333-3333-333333333332"


def _db():
    """Build a (dbcm, conn, cur) context-manager-shaped DB stub."""
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__ = lambda s: cur
    conn.cursor.return_value.__exit__ = lambda s, *a: False
    dbcm = MagicMock()
    dbcm.__enter__ = lambda s: conn
    dbcm.__exit__ = lambda s, *a: False
    return dbcm, conn, cur


def _run(users, claimed_by_user, rls_ok=True, delay_side_effect=None):
    """Run the reconciler, returning (result, enqueued, update_calls).

    ``claimed_by_user`` maps each user to the rows their claim UPDATE returns
    as (incident_id, aurora_chat_session_id) tuples — an empty list models a
    row that was not stuck (or was already claimed by the previous pass).
    """
    enqueued = []
    update_calls = []
    dbcm, _conn, cur = _db()

    def fake_execute(sql, params=None):
        if "UPDATE incidents" in sql:
            update_calls.append((sql, params))
            uid = params[0]
            cur.fetchall.return_value = claimed_by_user.get(uid, [])
        else:
            cur.fetchall.return_value = []

    cur.execute.side_effect = fake_execute

    def fake_delay(incident_id, user_id, session_id, announce_completion):
        enqueued.append((incident_id, user_id, session_id, announce_completion))

    with patch("utils.db.connection_pool.db_pool.get_admin_connection", return_value=dbcm), \
         patch.object(mod, "_affected_users", return_value=users), \
         patch.object(mod, "set_rls_context", return_value=rls_ok), \
         patch("chat.background.summarization.generate_incident_summary_from_chat") as summary_task:
        if delay_side_effect is not None:
            summary_task.delay.side_effect = delay_side_effect
        else:
            summary_task.delay.side_effect = fake_delay
        result = mod._reconcile_stuck_summarizing()
    return result, enqueued, update_calls


# --- happy path -------------------------------------------------------------

def test_stuck_incident_is_claimed_and_redriven():
    """The whole point: a 'summarizing' row past the stuck threshold gets its
    summary task re-enqueued with the incident's own user and chat session."""
    result, enqueued, update_calls = _run(
        [USER_A],
        {USER_A: [(INC_1, SESSION_1)]},
    )
    assert enqueued == [(INC_1, USER_A, SESSION_1, True)]
    assert result == {"re_driven": 1}
    # The claim restamps updated_at before the task is queued.
    assert len(update_calls) == 1
    assert "SET updated_at = NOW()" in update_calls[0][0]


def test_unclaimed_rows_are_skipped():
    """Nothing stuck (or everything already claimed): no task is queued."""
    result, enqueued, _ = _run([USER_A], {USER_A: []})
    assert enqueued == []
    assert result == {"re_driven": 0}


def test_multiple_users_each_get_their_own_claim():
    """Rows are claimed per user (RLS), and only claimed rows are re-driven."""
    result, enqueued, update_calls = _run(
        [USER_A, USER_B],
        {USER_A: [(INC_1, SESSION_1)], USER_B: []},
    )
    assert enqueued == [(INC_1, USER_A, SESSION_1, True)]
    assert result == {"re_driven": 1}
    assert [p[0] for _, p in update_calls] == [USER_A, USER_B]


# --- eligibility filters ------------------------------------------------------

def test_query_bounds_the_stuck_window_and_excludes_unsummable_rows():
    """Only 'summarizing', non-merged rows with a chat session, older than the
    stuck threshold and younger than the re-drive window, are claimed."""
    _result, _enqueued, update_calls = _run([USER_A], {USER_A: []})
    sql, params = update_calls[0]
    assert "aurora_status = 'summarizing'" in sql
    assert "status != 'merged'" in sql
    assert "merged_into_incident_id IS NULL" in sql
    assert "aurora_chat_session_id IS NOT NULL" in sql
    stuck_threshold, age_threshold = params[1], params[2]
    # Defaults: stuck after 30 minutes, re-drive window 7 days.
    assert stuck_threshold - age_threshold == timedelta(days=7) - timedelta(minutes=30)
    assert stuck_threshold <= datetime.now() - timedelta(minutes=29)


def test_env_knobs_override_the_window(monkeypatch):
    monkeypatch.setenv("SUMMARIZING_STUCK_AFTER_MINUTES", "60")
    monkeypatch.setenv("SUMMARIZING_REDRIVE_MAX_AGE_DAYS", "2")
    _result, _enqueued, update_calls = _run([USER_A], {USER_A: []})
    _sql, params = update_calls[0]
    assert params[1] - params[2] == timedelta(days=2) - timedelta(minutes=60)


def test_rls_failure_skips_the_user():
    """A user whose RLS context cannot be set is not swept at all."""
    result, enqueued, update_calls = _run(
        [USER_A],
        {USER_A: [(INC_1, SESSION_1)]},
        rls_ok=False,
    )
    assert update_calls == []
    assert enqueued == []
    assert result == {"re_driven": 0}


# --- failure containment ------------------------------------------------------

def test_enqueue_failure_does_not_abort_the_sweep():
    """One failed .delay() must not strand the remaining claimed rows."""
    calls = []

    def flaky_delay(incident_id, user_id, session_id, announce_completion):
        calls.append(incident_id)
        if len(calls) == 1:
            raise RuntimeError("broker down")

    result, _enqueued, _ = _run(
        [USER_A],
        {USER_A: [(INC_1, SESSION_1), (INC_2, SESSION_2)]},
        delay_side_effect=flaky_delay,
    )
    assert result == {"re_driven": 1}
    assert calls == [INC_1, INC_2]


def test_db_failure_returns_error_dict():
    """A broken pool never raises into beat; the sweep reports and exits."""
    with patch("utils.db.connection_pool.db_pool.get_admin_connection",
               side_effect=RuntimeError("pool exhausted")):
        result = mod._reconcile_stuck_summarizing()
    assert result["re_driven"] == 0
    assert "pool exhausted" in result["error"]
