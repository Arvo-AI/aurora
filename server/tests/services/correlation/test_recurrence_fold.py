"""fold_incident: root resolution, tie-breaks, idempotency — mocked cursors only.

Verdict-row param order (see insert_verdict): (incident_id, user_id, org_id,
decision_point, mode, claimed, accepted, reasoning, correlator_score, folded,
reject_reason, elapsed_ms, model) — assertions index [6]=accepted, [9]=folded,
[10]=reject_reason.
"""

import os
import sys
import types
import uuid
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

_server_dir = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))

from services.correlation.recurrence_fold import fold_incident  # noqa: E402

A = str(uuid.uuid4())
B = str(uuid.uuid4())
C = str(uuid.uuid4())

_T0 = datetime(2026, 8, 1, 12, 0, 0)


def _incident(status="investigating", recurrence_of=None, started_at=_T0):
    return {"status": status, "recurrence_of": recurrence_of, "started_at": started_at}


class FakeDB:
    def __init__(self, incidents):
        self.incidents = incidents
        self.group_stale = False
        self.primary_alert = ("datadog", 7, "High CPU", "api", "critical", {"m": 1})
        self.has_recurrence_row = False
        self.verdicts = []
        self.lifecycle = []
        self.alert_inserts = []
        self.anchor_updates = []
        self.locks = []
        self.on_lock = None  # fired once, on the first advisory lock — simulates a concurrent fold


class FakeCursor:
    def __init__(self, db: FakeDB):
        self.db = db
        self._result = None
        self.rowcount = 0

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        db = self.db
        if "SELECT id, status, recurrence_of_incident_id, started_at" in s:
            row = db.incidents.get(str(params[0]))
            self._result = (
                (str(params[0]), row["status"], row["recurrence_of"], row["started_at"])
                if row
                else None
            )
        elif "pg_advisory_xact_lock" in s:
            db.locks.append(params[0])
            if db.on_lock:
                hook, db.on_lock = db.on_lock, None
                hook()
            self._result = None
        elif "MAX(COALESCE(alert_fired_at, started_at))" in s:
            self._result = (db.group_stale,)
        elif "SET recurrence_of_incident_id = NULL" in s:
            row = db.incidents.get(str(params[0]))
            if row and row["recurrence_of"] == str(params[1]):
                row["recurrence_of"] = None
                self.rowcount = 1
            else:
                self.rowcount = 0
        elif "SET recurrence_of_incident_id = %s" in s:
            row = db.incidents.get(str(params[1]))
            if row and row["recurrence_of"] is None:
                row["recurrence_of"] = str(params[0])
                self.rowcount = 1
            else:
                self.rowcount = 0
        elif "correlation_strategy = 'primary'" in s:
            self._result = db.primary_alert
        elif "correlation_strategy = 'recurrence'" in s:
            self._result = (1,) if db.has_recurrence_row else None
        elif "INSERT INTO incident_alerts" in s:
            db.alert_inserts.append(params)
        elif "INSERT INTO recurrence_verdicts" in s:
            db.verdicts.append(params)
        elif "INSERT INTO incident_lifecycle_events" in s:
            db.lifecycle.append(params)
        elif "correlated_alert_count" in s:
            db.anchor_updates.append((s, params))
        else:
            self._result = None

    def fetchone(self):
        return self._result


def _fold(db, incident_id=B, claimed=A, monkeypatch=None, **overrides):
    cursor = FakeCursor(db)
    conn = MagicMock(name="conn")
    conn.cursor.return_value.__enter__.return_value = cursor
    pool = MagicMock(name="db_pool")
    pool.get_admin_connection.return_value.__enter__.return_value = conn

    sse_stub = types.ModuleType("routes.incidents_sse")
    sse_stub.broadcast_incident_update_to_user_connections = MagicMock()

    kwargs = dict(
        incident_id=incident_id,
        user_id="u1",
        claimed_recurrence_of=claimed,
        reasoning="same root cause",
        mode="live",
        correlator_score=0.7,
        elapsed_ms=1234,
        model="test-model",
    )
    kwargs.update(overrides)
    with patch("services.correlation.recurrence_fold.db_pool", pool), patch(
        "services.correlation.recurrence_fold.set_rls_context", return_value="org-1"
    ), patch.dict(sys.modules, {"routes.incidents_sse": sse_stub}):
        result = fold_incident(**kwargs)
    return result, conn


class TestBasicFold:
    def test_fold_writes_pointer_verdict_alert_and_lifecycle(self):
        db = FakeDB({A: _incident(), B: _incident(started_at=_T0 + timedelta(minutes=7))})
        result, conn = _fold(db)
        assert result.folded is True
        assert result.root_id == A
        assert db.incidents[B]["recurrence_of"] == A
        assert len(db.verdicts) == 1
        v = db.verdicts[0]
        assert v[6] == A          # accepted_recurrence_of
        assert v[9] is True       # folded
        assert v[10] is None      # reject_reason
        assert len(db.lifecycle) == 1
        assert len(db.alert_inserts) == 1
        assert db.alert_inserts[0][8] == "recurrence"  # correlation_strategy
        assert conn.commit.called

    def test_anchor_update_guards_null_affected_services(self):
        db = FakeDB({A: _incident(), B: _incident()})
        _fold(db)
        assert len(db.anchor_updates) == 1
        sql, _ = db.anchor_updates[0]
        assert "WHEN affected_services IS NULL THEN ARRAY[%s]" in sql

    def test_existing_recurrence_row_not_duplicated(self):
        db = FakeDB({A: _incident(), B: _incident()})
        db.has_recurrence_row = True
        result, _ = _fold(db)
        assert result.folded is True
        assert db.alert_inserts == []

    def test_advisory_locks_taken_in_sorted_pair(self):
        db = FakeDB({A: _incident(), B: _incident()})
        _fold(db)
        assert len(db.locks) == 2
        assert db.locks == sorted(db.locks)


class TestRootResolution:
    def test_claimed_child_of_root_resolves_to_root(self):
        # Agent names B's sibling C (already folded into A) — fold lands on A.
        db = FakeDB({A: _incident(), C: _incident(recurrence_of=A), B: _incident()})
        result, _ = _fold(db, claimed=C)
        assert result.folded is True
        assert result.root_id == A
        assert db.incidents[B]["recurrence_of"] == A

    def test_malformed_id_rejects_invalid_id(self):
        db = FakeDB({B: _incident()})
        result, _ = _fold(db, claimed="not-a-uuid")
        assert result.folded is False
        assert result.reject_reason == "invalid_id"
        assert db.verdicts[0][10] == "invalid_id"

    def test_unknown_id_rejects_invalid_id(self):
        db = FakeDB({B: _incident()})
        result, _ = _fold(db, claimed=str(uuid.uuid4()))
        assert result.folded is False
        assert result.reject_reason == "invalid_id"

    def test_self_reference_rejected(self):
        db = FakeDB({B: _incident()})
        result, _ = _fold(db, claimed=B)
        assert result.folded is False
        assert result.reject_reason == "self_reference"
        assert db.incidents[B]["recurrence_of"] is None

    def test_merged_target_rejected(self):
        db = FakeDB({A: _incident(status="merged"), B: _incident()})
        result, _ = _fold(db)
        assert result.folded is False
        assert result.reject_reason == "merged_target"

    def test_merged_child_rejected(self):
        # Guard carried over from the removed manual-merge route: a child
        # already merged elsewhere must not also join a recurrence group.
        db = FakeDB({A: _incident(), B: _incident(status="merged")})
        result, _ = _fold(db)
        assert result.folded is False
        assert result.reject_reason == "merged_child"
        assert db.incidents[B]["recurrence_of"] is None


class TestEligibility:
    def test_stale_group_rejected(self):
        db = FakeDB({A: _incident(), B: _incident()})
        db.group_stale = True
        result, _ = _fold(db)
        assert result.folded is False
        assert result.reject_reason == "stale_group"
        assert db.incidents[B]["recurrence_of"] is None


class TestConcurrency:
    def test_already_folded_is_noop(self):
        # Re-fold of an already-folded child (e.g. Celery retry racing) — no
        # pointer overwrite, verdict records the reject.
        db = FakeDB({A: _incident(), B: _incident(recurrence_of=A)})
        result, _ = _fold(db)
        assert result.folded is False
        assert result.reject_reason == "already_folded"
        assert result.root_id == A
        assert db.incidents[B]["recurrence_of"] == A

    def test_mutual_fold_root_earlier_wins_anchor(self):
        # Concurrent fold pointed A at B between resolve and lock; A started
        # earlier so A stays anchor: its pointer is nulled, B folds into A.
        db = FakeDB({A: _incident(started_at=_T0), B: _incident(started_at=_T0 + timedelta(minutes=5))})
        db.on_lock = lambda: db.incidents[A].__setitem__("recurrence_of", B)
        result, _ = _fold(db)
        assert result.folded is True
        assert result.root_id == A
        assert db.incidents[A]["recurrence_of"] is None
        assert db.incidents[B]["recurrence_of"] == A

    def test_mutual_fold_child_earlier_loses(self):
        db = FakeDB({A: _incident(started_at=_T0 + timedelta(minutes=5)), B: _incident(started_at=_T0)})
        db.on_lock = lambda: db.incidents[A].__setitem__("recurrence_of", B)
        result, _ = _fold(db)
        assert result.folded is False
        assert result.reject_reason == "mutual_fold_lost"
        # The concurrent fold's pointer stands.
        assert db.incidents[A]["recurrence_of"] == B
        assert db.incidents[B]["recurrence_of"] is None

    def test_root_folded_elsewhere_is_chased(self):
        # A gets folded into C while we wait on the lock — fold follows to C.
        db = FakeDB({A: _incident(), C: _incident(), B: _incident()})
        db.on_lock = lambda: db.incidents[A].__setitem__("recurrence_of", C)
        result, _ = _fold(db)
        assert result.folded is True
        assert result.root_id == C
        assert db.incidents[B]["recurrence_of"] == C


class TestRLSMiss:
    def test_missing_org_rejects_error_without_writes(self):
        db = FakeDB({A: _incident(), B: _incident()})
        cursor = FakeCursor(db)
        conn = MagicMock(name="conn")
        conn.cursor.return_value.__enter__.return_value = cursor
        pool = MagicMock(name="db_pool")
        pool.get_admin_connection.return_value.__enter__.return_value = conn
        with patch("services.correlation.recurrence_fold.db_pool", pool), patch(
            "services.correlation.recurrence_fold.set_rls_context", return_value=None
        ):
            result = fold_incident(
                incident_id=B,
                user_id="u1",
                claimed_recurrence_of=A,
                reasoning="r",
                mode="live",
            )
        assert result.folded is False
        assert result.reject_reason == "error"
        assert db.verdicts == []
        assert db.incidents[B]["recurrence_of"] is None
