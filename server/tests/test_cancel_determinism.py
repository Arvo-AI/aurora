"""Tests for Phase 6 cancel determinism.

Two correctness invariants are exercised here:

1. The chat_events partial UNIQUE on (message_id, type IN terminal) makes a
   second terminal write a no-op. This is what lets the WS cancel handler
   write `assistant_interrupted` even if the workflow already wrote
   `assistant_finalized` (or vice versa) — the second write returns seq=0
   and is silently dropped.

2. cancel_rca_for_incident must flip aurora_status='cancelled' BEFORE it
   revokes the Celery task. If the worker dies (SIGTERM, OOM) before the
   row is updated, the row stays stuck on 'running' forever. The test
   asserts that revoke is observed only after the UPDATE has committed.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import psycopg2
import pytest


# ---------------------------------------------------------------------------
# 1. Terminal-event idempotency on concurrent finalize+interrupt
# ---------------------------------------------------------------------------


def test_concurrent_finalize_and_interrupt_second_returns_zero():
    """The first terminal write wins; the second hits the partial UNIQUE
    `uq_chat_events_terminal_per_msg` and returns seq=0 (no-op)."""
    # Drop any stub modules left over from sibling tests.
    for mod in (
        "chat.backend.agent.utils.persistence.chat_events",
        "utils.redis.redis_stream_bus",
    ):
        sys.modules.pop(mod, None)

    from chat.backend.agent.utils.persistence import chat_events as ce

    # Mock the connection pool to simulate: first call succeeds with seq=42,
    # second call raises IntegrityError on the partial UNIQUE constraint.
    call_count = {"n": 0}

    class _FakeCursor:
        def __init__(self):
            self._fetch = None

        def execute(self, sql, args=None):
            if "INSERT INTO chat_events" in sql:
                call_count["n"] += 1
                if call_count["n"] == 1:
                    self._fetch = (42,)
                else:
                    raise psycopg2.IntegrityError(
                        "duplicate key value violates unique constraint "
                        "\"uq_chat_events_terminal_per_msg\""
                    )

        def fetchone(self):
            return self._fetch

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

        def commit(self):
            pass

        def rollback(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    class _FakePool:
        @staticmethod
        def get_admin_connection():
            return _FakeConn()

    fake_db_pool = MagicMock()
    fake_db_pool.db_pool = _FakePool()

    with patch.dict(sys.modules, {"utils.db.connection_pool": fake_db_pool}):
        seq1 = ce._insert_event_sync(
            session_id="s1",
            org_id="o1",
            type_="assistant_finalized",
            payload={},
            message_id="m1",
            agent_id="main",
            parent_agent_id=None,
            payload_schema_version=1,
        )
        seq2 = ce._insert_event_sync(
            session_id="s1",
            org_id="o1",
            type_="assistant_interrupted",
            payload={"reason": "user_cancelled"},
            message_id="m1",
            agent_id="main",
            parent_agent_id=None,
            payload_schema_version=1,
        )

    assert seq1 == 42, "first writer must win with a real seq"
    assert seq2 == 0, "second terminal writer must be a no-op (seq=0)"


# ---------------------------------------------------------------------------
# 2. cancel_rca_for_incident: DB flip BEFORE Celery revoke
# ---------------------------------------------------------------------------


def test_cancel_rca_flips_status_before_revoke(monkeypatch):
    """cancel_rca_for_incident must commit aurora_status='cancelled' BEFORE
    it calls celery_app.control.revoke. We verify by recording the event
    order with a shared list. If the worker crashes after revoke but before
    the row commit, aurora_status would stay stuck on 'running' forever.
    """
    events: list[str] = []

    class _RecordingCursor:
        def __init__(self):
            self._fetch = None

        def execute(self, sql, args=None):
            if "WITH target" in sql and "UPDATE incidents" in sql:
                events.append("flip_executed")
                self._fetch = ("celery-task-1", "session-1", "org-1")
            elif "SET myapp.current_org_id" in sql:
                pass

        def fetchone(self):
            return self._fetch

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    class _RecordingConn:
        def cursor(self):
            return _RecordingCursor()

        def commit(self):
            events.append("flip_committed")

        def rollback(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    class _Pool:
        @staticmethod
        def get_admin_connection():
            return _RecordingConn()

    fake_celery = MagicMock()

    def _record_revoke(task_id, terminate=False, signal=None):
        events.append(f"revoke:{task_id}:{signal}")

    fake_celery.control.revoke = _record_revoke

    # Import lazily: if the module fails to import in this env, skip the test.
    try:
        import importlib
        task_mod = importlib.import_module("chat.background.task")
    except Exception as e:  # pragma: no cover
        pytest.skip(f"chat.background.task unavailable in test env: {e}")

    monkeypatch.setattr(task_mod, "celery_app", fake_celery, raising=False)
    monkeypatch.setattr(task_mod, "db_pool", _Pool(), raising=False)
    monkeypatch.setattr(
        task_mod, "set_rls_context", lambda *a, **kw: True, raising=False
    )

    # Stub the post-flip best-effort writers so they're no-ops and never block.
    async def _noop_active(*a, **kw):
        return None

    async def _noop_record(*a, **kw):
        return 0

    async def _noop_get_async_redis(*a, **kw):
        return None

    monkeypatch.setattr(
        "chat.backend.agent.utils.persistence.chat_events.get_active_stream_id",
        _noop_active,
        raising=False,
    )
    monkeypatch.setattr(
        "chat.backend.agent.utils.persistence.chat_events.record_event",
        _noop_record,
        raising=False,
    )
    monkeypatch.setattr(
        "utils.redis.redis_stream_bus.get_async_redis",
        _noop_get_async_redis,
        raising=False,
    )

    result = task_mod.cancel_rca_for_incident("incident-1", "user-1")

    assert result is True
    flip_committed_idx = events.index("flip_committed")
    revoke_idx = next(i for i, e in enumerate(events) if e.startswith("revoke:"))
    assert flip_committed_idx < revoke_idx, (
        f"Expected flip_committed before revoke, got: {events}"
    )
