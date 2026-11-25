"""Alert RCAs over the org's budget are queued and grouped, never dropped.

* A burst of 34 alerts leaves no incident in error: each is investigated,
  grouped into another investigation, or queued with a reason, and the queue
  drains until every one is covered.
* An org under the limit is dispatched at once, as before.
* A burst still arriving is not drained; a settled one becomes one investigation.
* A failed start is queued for retry, with its group.
* Housekeeping runs (prediscovery, actions) do not spend the alert budget.
* The stale sweep skips queued and grouped incidents but still fails a
  dispatched one that was lost.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import chat.background.rca_dispatch as rd

ORG = "org-1"
USER = "user-1"
T0 = datetime(2026, 11, 1, 12, 0, 0)


class FakeStore:
    """The queue's storage in memory, with the same semantics as SqlStore."""

    def __init__(self):
        self.org_id = ORG
        self.clock = T0
        self.incidents: dict[str, dict] = {}
        self.attached: dict[str, list[str]] = {}

    # --- test helpers
    def add(self, incident_id, severity="medium", source="sentry"):
        self.incidents[incident_id] = {
            "aurora_status": "idle", "status": "investigating", "source": source,
            "severity": severity, "title": f"Issue {incident_id}", "service": "api",
            "dispatched_at": None, "queued_at": None, "reason": None,
            "request": None, "leader": None, "task": None,
        }

    def by_status(self, status):
        return [i for i, inc in self.incidents.items() if inc["aurora_status"] == status]

    # --- SqlStore interface
    def commit(self):
        pass

    def rollback(self):
        pass

    def now(self):
        return self.clock

    def lock_org(self):
        pass

    def lock_incident(self, incident_id):
        inc = self.incidents.get(incident_id)
        return inc["aurora_status"] if inc else None

    def dispatched_recently(self, incident_id):
        at = self.incidents[incident_id]["dispatched_at"]
        return bool(at and at > self.clock - timedelta(seconds=rd._REDISPATCH_GUARD_SECONDS))

    def usage(self):
        now = self.clock
        dispatched = [i for i in self.incidents.values() if i["dispatched_at"]]
        return rd.BudgetUsage(
            started_in_window=sum(i["dispatched_at"] > now - timedelta(seconds=rd.WINDOW_SECONDS) for i in dispatched),
            started_in_hour=sum(i["dispatched_at"] > now - timedelta(hours=1) for i in dispatched),
            in_flight=sum(
                i["aurora_status"] in ("running", "summarizing")
                or (i["aurora_status"] == "idle" and i["dispatched_at"] > now - timedelta(minutes=30))
                for i in dispatched
            ),
        )

    def has_queued(self):
        return bool(self.by_status(rd.QUEUED))

    def queue(self, incident_id, reason, request):
        inc = self.incidents[incident_id]
        inc.update(aurora_status=rd.QUEUED, reason=reason, leader=None)
        inc["queued_at"] = inc["queued_at"] or self.clock
        inc["request"] = request or inc["request"]

    def refresh_request(self, incident_id, request):
        self.incidents[incident_id]["request"] = request

    def mark_dispatched(self, incident_id):
        self.incidents[incident_id].update(
            aurora_status="idle", dispatched_at=self.clock, reason=None, queued_at=None, leader=None,
            request=None,
        )

    def mark_grouped(self, follower_ids, leader_id):
        for f in follower_ids:
            self.incidents[f].update(aurora_status=rd.GROUPED, reason=rd.REASON_BURST, leader=leader_id)
        self.attached.setdefault(leader_id, []).extend(follower_ids)

    def requeue_group(self, leader_id):
        for inc in self.incidents.values():
            if inc["leader"] == leader_id and inc["aurora_status"] == rd.GROUPED:
                inc.update(aurora_status=rd.QUEUED, reason=rd.REASON_DISPATCH_FAILED, leader=None)
                inc["queued_at"] = inc["queued_at"] or self.clock
        self.attached.pop(leader_id, None)

    def queued(self):
        rows = [
            rd.QueuedIncident(
                id=i, user_id=USER, source_type=inc["source"], title=inc["title"],
                service=inc["service"], severity=inc["severity"], queued_at=inc["queued_at"],
                request=inc["request"],
            )
            for i, inc in self.incidents.items()
            if inc["aurora_status"] == rd.QUEUED and inc["status"] == "investigating" and inc["request"]
        ]
        return sorted(rows, key=lambda q: (q.queued_at, q.id))

    def set_reason(self, incident_ids, reason):
        for i in incident_ids:
            if self.incidents[i]["aurora_status"] == rd.QUEUED:
                self.incidents[i]["reason"] = reason

    def set_task(self, incident_id, task_id):
        self.incidents[incident_id]["task"] = task_id


@pytest.fixture
def store():
    return FakeStore()


@pytest.fixture
def started(monkeypatch, store):
    """Starting an RCA puts it on a worker, which marks the incident running."""
    calls = []

    def _start(user_id, incident_id, request, prompt=None):
        calls.append(SimpleNamespace(incident_id=incident_id, prompt=prompt or request.prompt))
        store.incidents[incident_id]["aurora_status"] = "running"
        return f"task-{incident_id}"

    monkeypatch.setattr(rd, "start_rca", _start)
    return calls


def _request(incident_id):
    return rd.RcaRequest(
        source="sentry", prompt=f"Investigate {incident_id}", rail_text=f"Issue {incident_id}",
        session_title=f"RCA: Issue {incident_id}", trigger_metadata={"source": "sentry"},
    )


def _alert(store, incident_id, **kw):
    store.add(incident_id, **kw)
    return rd.dispatch_with_store(store, USER, incident_id, _request(incident_id))


def _finish_old_rcas(store, minutes=10):
    """Investigations take `minutes`; finished ones complete."""
    for inc in store.incidents.values():
        if inc["aurora_status"] == "running" and inc["dispatched_at"] <= store.clock - timedelta(minutes=minutes):
            inc["aurora_status"] = "complete"
            inc["status"] = "analyzed"


def _covered(store, incident_id):
    inc = store.incidents[incident_id]
    if inc["aurora_status"] == rd.GROUPED:
        return store.incidents[inc["leader"]]["dispatched_at"] is not None
    return inc["dispatched_at"] is not None


# --- The burst ----------------------------------------------------------------------

def test_a_burst_of_34_alerts_leaves_none_in_error_and_every_one_is_covered(store, started):
    # 34 distinct issues over 15 minutes, as a new org connecting Sentry saw.
    ids = [f"inc-{n:02d}" for n in range(34)]
    for n, incident_id in enumerate(ids):
        store.clock = T0 + timedelta(seconds=n * 26)
        outcome = _alert(store, incident_id)
        assert outcome in (rd.DISPATCHED, rd.QUEUED_OUTCOME)
        if outcome == rd.QUEUED_OUTCOME:
            assert store.incidents[incident_id]["reason"] in rd.QUEUE_REASONS
        if n % 2 == 0:
            rd.drain_with_store(store)

    assert store.by_status("error") == []
    for incident_id in ids:
        status = store.incidents[incident_id]["aurora_status"]
        assert status in ("running", rd.GROUPED, rd.QUEUED)

    # The drain keeps running every minute; investigations finish.
    for minute in range(1, 120):
        store.clock = T0 + timedelta(minutes=15 + minute)
        _finish_old_rcas(store)
        rd.drain_with_store(store)

    assert store.by_status(rd.QUEUED) == []
    assert store.by_status("error") == []
    assert all(_covered(store, i) for i in ids)
    # Grouping collapsed the burst: far fewer investigations than alerts.
    assert len(started) < 15, len(started)


def test_the_first_alerts_of_a_burst_still_start_at_once(store, started):
    outcomes = [_alert(store, f"inc-{n}") for n in range(rd.MAX_PER_WINDOW + 1)]
    assert outcomes[: rd.MAX_PER_WINDOW] == [rd.DISPATCHED] * rd.MAX_PER_WINDOW
    assert outcomes[-1] == rd.QUEUED_OUTCOME
    assert store.incidents[f"inc-{rd.MAX_PER_WINDOW}"]["reason"] == rd.REASON_RATE


def test_an_org_under_the_limit_is_dispatched_at_once(store, started):
    for n in range(3):
        store.clock = T0 + timedelta(minutes=n)
        assert _alert(store, f"inc-{n}") == rd.DISPATCHED
    assert [c.incident_id for c in started] == ["inc-0", "inc-1", "inc-2"]
    assert store.by_status(rd.QUEUED) == []


def test_a_new_alert_joins_alerts_already_waiting(store, started):
    for n in range(rd.MAX_PER_WINDOW + 1):
        _alert(store, f"inc-{n}")
    store.clock = T0 + timedelta(minutes=6)  # the window reopened
    assert _alert(store, "late") == rd.QUEUED_OUTCOME
    assert store.incidents["late"]["reason"] == rd.REASON_BURST


def test_a_burst_still_arriving_is_not_drained(store, started):
    for n in range(rd.MAX_PER_WINDOW + 3):
        _alert(store, f"inc-{n}")
    _finish_old_rcas(store, minutes=0)
    store.clock = T0 + timedelta(seconds=rd.SETTLE_SECONDS - 1)
    assert rd.drain_with_store(store) == {"dispatched": 0, "grouped": 0}


def test_a_settled_burst_becomes_one_investigation_led_by_the_most_severe_alert(store, started):
    for n in range(rd.MAX_PER_WINDOW):
        _alert(store, f"inc-{n}")
    _alert(store, "low", severity="low")
    _alert(store, "crit", severity="critical")
    _alert(store, "med", severity="medium")
    _finish_old_rcas(store, minutes=0)
    store.clock = T0 + timedelta(minutes=10)

    assert rd.drain_with_store(store) == {"dispatched": 1, "grouped": 2}
    assert started[-1].incident_id == "crit"
    assert store.attached["crit"] == ["low", "med"]
    assert store.incidents["low"]["leader"] == "crit"
    assert "Related alerts in the same burst" in started[-1].prompt
    assert "Issue low" in started[-1].prompt


def test_the_grouped_prompt_is_bounded_however_large_the_burst():
    leader = _request("lead")
    followers = [
        rd.QueuedIncident(id=f"f{n}", user_id=USER, source_type="sentry", title="x" * 1000,
                          service=None, severity=None, queued_at=T0, request=None)
        for n in range(200)
    ]
    prompt = rd.grouped_prompt(leader, followers)
    assert len(prompt) < 6000
    assert "...and 175 more" in prompt


def test_a_drain_over_budget_says_why_the_alerts_still_wait(store, started):
    for n in range(rd.MAX_PER_WINDOW + 2):
        _alert(store, f"inc-{n}")
    store.clock = T0 + timedelta(minutes=10)  # settled, but 5 are still running
    store.incidents["inc-0"]["aurora_status"] = "running"
    for n in range(rd.MAX_CONCURRENT - rd.MAX_PER_WINDOW):
        store.add(f"extra-{n}")
        store.incidents[f"extra-{n}"].update(aurora_status="running", dispatched_at=T0)

    assert rd.drain_with_store(store)["dispatched"] == 0
    assert store.incidents[f"inc-{rd.MAX_PER_WINDOW}"]["reason"] == rd.REASON_CONCURRENCY


def test_a_failed_start_is_queued_for_retry_with_its_group(monkeypatch, store, started):
    for n in range(rd.MAX_PER_WINDOW):
        _alert(store, f"inc-{n}")
    _alert(store, "a")
    _alert(store, "b")
    _finish_old_rcas(store, minutes=0)
    store.clock = T0 + timedelta(minutes=10)

    def _broken(*_a, **_k):
        raise RuntimeError("broker down")

    monkeypatch.setattr(rd, "start_rca", _broken)
    rd.drain_with_store(store)

    assert sorted(store.by_status(rd.QUEUED)) == ["a", "b"]
    assert store.incidents["a"]["reason"] == rd.REASON_DISPATCH_FAILED
    assert "a" not in store.attached


def test_a_queued_alert_that_fires_again_keeps_its_place(store, started):
    for n in range(rd.MAX_PER_WINDOW + 1):
        _alert(store, f"inc-{n}")
    queued_id = f"inc-{rd.MAX_PER_WINDOW}"
    first_queued_at = store.incidents[queued_id]["queued_at"]
    store.clock = T0 + timedelta(minutes=1)
    assert rd.dispatch_with_store(store, USER, queued_id, _request(queued_id)) == rd.QUEUED_OUTCOME
    assert store.incidents[queued_id]["queued_at"] == first_queued_at
    assert len(started) == rd.MAX_PER_WINDOW


def test_a_retried_delivery_does_not_start_a_second_rca(store, started):
    assert _alert(store, "inc-1") == rd.DISPATCHED
    assert rd.dispatch_with_store(store, USER, "inc-1", _request("inc-1")) == rd.DISPATCHED
    assert len(started) == 1


@pytest.mark.parametrize("usage, reason", [
    (rd.BudgetUsage(0, 0, 0), None),
    (rd.BudgetUsage(rd.MAX_PER_WINDOW, 0, 0), rd.REASON_RATE),
    (rd.BudgetUsage(0, rd.MAX_PER_HOUR, 0), rd.REASON_HOURLY),
    (rd.BudgetUsage(0, 0, rd.MAX_CONCURRENT), rd.REASON_CONCURRENCY),
])
def test_the_budget(usage, reason):
    assert rd.budget_refusal(usage) == reason


# --- Housekeeping does not spend the alert budget -------------------------------------

def test_housekeeping_runs_do_not_spend_the_alert_budget(monkeypatch, store, started):
    import chat.background.task as task_mod

    counts: dict[str, int] = {}
    fake_redis = SimpleNamespace(
        incr=lambda k: counts.__setitem__(k, counts.get(k, 0) + 1) or counts[k],
        expire=lambda *_a: None,
        ttl=lambda _k: 100,
    )
    monkeypatch.setattr(task_mod, "_redis_client", fake_redis)
    # Actions, team routing and postmortems exhaust their own limiter...
    for _ in range(20):
        task_mod.is_background_chat_allowed(USER)
    assert task_mod.is_background_chat_allowed(USER) is False
    # ...and an alert still starts its RCA.
    assert _alert(store, "inc-1") == rd.DISPATCHED


def test_a_prediscovery_run_does_not_consume_the_alert_rca_budget(monkeypatch, store, started):
    import importlib

    # sys.modules entries, the ones prediscovery's local imports resolve to.
    pre = importlib.import_module("chat.background.prediscovery_task")
    if isinstance(pre.run_prediscovery, MagicMock):
        # Without Celery installed, conftest stubs it and @celery_app.task
        # returns a MagicMock in place of the task, so there is no body to run.
        pytest.skip("needs the real Celery task decorator")
    task_mod = importlib.import_module("chat.background.task")
    liveness = importlib.import_module("chat.background.liveness")
    prompt_builder = importlib.import_module("chat.background.rca_prompt_builder")
    auth = importlib.import_module("utils.auth.stateless_auth")
    hooks = importlib.import_module("utils.hooks")

    limiter = MagicMock(return_value=True)
    dispatch = MagicMock()
    monkeypatch.setattr(task_mod, "is_background_chat_allowed", limiter)
    monkeypatch.setattr(rd, "dispatch_alert_rca", dispatch)
    monkeypatch.setattr(auth, "get_org_id_for_user", lambda _u: ORG)
    monkeypatch.setattr(hooks, "get_hook", lambda _n: lambda *_a: (True, None))
    monkeypatch.setattr(prompt_builder, "get_user_providers", lambda _u: ["gcp"])
    monkeypatch.setattr(task_mod, "_get_connected_integrations", lambda _u: {"sentry": True})
    monkeypatch.setattr(pre, "build_prediscovery_prompt", lambda *_a: "discover")
    monkeypatch.setattr(task_mod, "create_background_chat_session", lambda **_k: "sess-1")
    monkeypatch.setattr(task_mod, "_execute_background_chat", MagicMock())
    monkeypatch.setattr(task_mod, "_update_session_status", lambda *_a, **_k: None)
    monkeypatch.setattr(liveness, "run_watched", lambda *_a, **_k: None)

    before = store.usage()
    result = pre.run_prediscovery.run(USER, trigger="new_connector")

    assert result["status"] == "completed", result
    limiter.assert_not_called()
    dispatch.assert_not_called()
    assert store.usage() == before
    # The alert budget is untouched: a full window of alerts still starts at once.
    assert [_alert(store, f"inc-{n}") for n in range(rd.MAX_PER_WINDOW)] == [rd.DISPATCHED] * rd.MAX_PER_WINDOW


# --- The stale sweep ------------------------------------------------------------------

DB_NOW = datetime(2026, 11, 1, 13, 0, 0)


class _SweepCursor:
    """Answers the sweep's queries from canned rows, keyed by a SQL fragment."""

    def __init__(self, results):
        self.results = results
        self.executed = []
        self._last = ""

    def execute(self, sql, params=None):
        self._last = " ".join(sql.split())
        self.executed.append((self._last, params))

    def _rows(self):
        if "LOCALTIMESTAMP" in self._last and self._last.startswith("SELECT LOCALTIMESTAMP"):
            return [(DB_NOW,)]
        if "SELECT DISTINCT id FROM users" in self._last:
            return [("u1",)]
        for key, rows in self.results.items():
            if key in self._last:
                return rows
        return []

    def fetchall(self):
        return list(self._rows())

    def fetchone(self):
        rows = self._rows()
        return rows[0] if rows else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _sweep(monkeypatch, results):
    import chat.background.task as task_mod

    cursor = _SweepCursor(results)
    conn = MagicMock()
    conn.cursor.return_value = cursor
    pool = MagicMock()
    pool.get_admin_connection.return_value.__enter__.return_value = conn
    monkeypatch.setattr(task_mod, "db_pool", pool)
    monkeypatch.setattr(task_mod, "set_rls_context", lambda *a, **k: ORG)
    monkeypatch.setattr(task_mod, "_record_rca_error", lambda *a, **k: None)
    reported = []
    monkeypatch.setattr(task_mod, "_report_or_fail_dead_rca", lambda *a, **k: reported.append(a[1]))
    task_mod._sweep_stale_background_chats()
    return cursor, reported


def _errored(cursor):
    return [p[0] for sql, p in cursor.executed if sql.startswith("UPDATE incidents SET aurora_status = 'error'")]


def test_the_never_started_sweep_skips_queued_and_grouped_incidents(monkeypatch):
    cursor, reported = _sweep(monkeypatch, {
        # Even if a queued or grouped row were returned, it is not failed.
        "WHERE i.status = 'investigating' AND i.updated_at < %s": [
            ("queued-inc", "queued", None, None),
            ("grouped-inc", "grouped", None, None),
        ],
    })
    sql = next(s for s, _ in cursor.executed if "WHERE i.status = 'investigating' AND i.updated_at < %s" in s)
    assert "NOT IN ('queued', 'grouped')" in sql
    assert _errored(cursor) == []
    assert reported == []


def test_the_never_started_sweep_still_fails_a_dispatched_rca_that_was_lost(monkeypatch):
    cursor, reported = _sweep(monkeypatch, {
        "WHERE i.status = 'investigating' AND i.updated_at < %s": [("lost-inc", "idle", None, None)],
        "UPDATE incidents SET aurora_status = 'error'": [("lost-inc",)],
    })
    assert _errored(cursor) == ["lost-inc"]
    assert reported == ["lost-inc"]


def test_grouped_incidents_are_analyzed_when_their_investigation_ends(monkeypatch):
    cursor, _ = _sweep(monkeypatch, {})
    sql = next(s for s, _ in cursor.executed if s.startswith("UPDATE incidents f SET status = 'analyzed'"))
    assert "l.aurora_status IN ('complete', 'error')" in sql
    assert "f.aurora_status = 'grouped'" in sql
    requeue = next(s for s, _ in cursor.executed if "rca_group_leader_id IS NULL" in s)
    assert "SET aurora_status = 'queued'" in requeue
