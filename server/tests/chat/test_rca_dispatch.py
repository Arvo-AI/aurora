"""Alert RCAs over the org's budget are queued, never dropped.

* A burst of 34 alerts leaves no incident in error: each is investigated or
  queued with a reason, and the queue drains until every one has had its own RCA.
* An org under the limit is dispatched at once, as before.
* The queue drains most severe first, then oldest first.
* A failed start is queued for retry and does not hold back the others.
* Housekeeping runs (prediscovery, actions) do not spend the alert budget.
* The stale sweep skips queued incidents but still fails a dispatched one
  that was lost, and requeues a reserved start that was never published.
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
        self.queue_reads = 0

    # --- test helpers
    def add(self, incident_id, severity="medium", source="sentry"):
        self.incidents[incident_id] = {
            "aurora_status": "idle", "status": "investigating", "source": source,
            "severity": severity, "dispatched_at": None, "queued_at": None, "reason": None,
            "request": None, "task": None,
        }

    def by_status(self, status):
        return [i for i, inc in self.incidents.items() if inc["aurora_status"] == status]

    # --- SqlStore interface
    def commit(self):
        pass

    def rollback(self):
        pass

    def lock_org(self):
        pass

    def lock_incident(self, incident_id):
        inc = self.incidents.get(incident_id)
        return inc["aurora_status"] if inc else None

    def dispatched_recently(self, incident_id):
        at = self.incidents[incident_id]["dispatched_at"]
        return bool(at and at > self.clock - timedelta(seconds=rd._REDISPATCH_GUARD_SECONDS))

    def usage(self):
        # Same predicates as SqlStore.usage: starts are counted from
        # rca_dispatched_at; in-flight counts every running RCA on an incident,
        # alert-driven or not (a chat-triggered RCA costs the same).
        now = self.clock
        dispatched = [i for i in self.incidents.values() if i["dispatched_at"]]
        return rd.BudgetUsage(
            started_in_window=sum(i["dispatched_at"] > now - timedelta(seconds=rd.WINDOW_SECONDS) for i in dispatched),
            started_in_hour=sum(i["dispatched_at"] > now - timedelta(hours=1) for i in dispatched),
            in_flight=sum(
                bool(i["aurora_status"] in ("running", "summarizing")
                     or (i["aurora_status"] == "idle" and i["dispatched_at"]
                         and i["dispatched_at"] > now - timedelta(minutes=30)))
                for i in self.incidents.values()
                if i["status"] != "merged"
            ),
        )

    def has_queued(self):
        return bool(self.by_status(rd.QUEUED))

    def queue(self, incident_id, reason, request):
        inc = self.incidents[incident_id]
        inc.update(aurora_status=rd.QUEUED, reason=reason)
        inc["queued_at"] = inc["queued_at"] or self.clock
        inc["request"] = request or inc["request"]

    def refresh_request(self, incident_id, request):
        self.incidents[incident_id]["request"] = request

    def mark_dispatched(self, incident_id, request=None):
        inc = self.incidents[incident_id]
        inc.update(aurora_status="idle", dispatched_at=self.clock, reason=None, queued_at=None, task=None)
        inc["request"] = request or inc["request"]

    def release_dispatch(self, incident_id):
        self.incidents[incident_id]["dispatched_at"] = None

    def next_queued(self, skip_ids):
        self.queue_reads += 1
        rows = [
            rd.QueuedIncident(
                id=i, user_id=USER, source_type=inc["source"], severity=inc["severity"],
                queued_at=inc["queued_at"], request=inc["request"],
            )
            for i, inc in self.incidents.items()
            if inc["aurora_status"] == rd.QUEUED and inc["status"] == "investigating"
            and inc["request"] and i not in skip_ids
        ]
        return min(rows, key=rd.drain_order) if rows else None

    def set_reason_all(self, reason):
        for inc in self.incidents.values():
            if inc["aurora_status"] == rd.QUEUED:
                inc["reason"] = reason

    def set_task(self, incident_id, task_id):
        self.incidents[incident_id].update(task=task_id, request=None)


@pytest.fixture
def store():
    return FakeStore()


@pytest.fixture
def started(monkeypatch, store):
    """Starting an RCA puts it on a worker, which marks the incident running."""
    calls = []

    def _start(user_id, incident_id, request, org_id=None):
        calls.append(SimpleNamespace(incident_id=incident_id, prompt=request.prompt, org_id=org_id))
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


def _fill_window_then_queue(store, *ids, **kw):
    """Spend the 5-minute window, queue *ids*, then let the window and the RCAs end."""
    for n in range(rd.MAX_PER_WINDOW):
        _alert(store, f"inc-{n}")
    for incident_id in ids:
        _alert(store, incident_id, **kw)
    _finish_old_rcas(store, minutes=0)
    store.clock = T0 + timedelta(minutes=10)


# --- The burst ----------------------------------------------------------------------

def test_a_burst_of_34_alerts_leaves_none_in_error_and_each_gets_its_own_rca(store, started):
    # 34 distinct issues over 15 minutes, as a new org connecting an error tracker saw.
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
        assert store.incidents[incident_id]["aurora_status"] in ("running", rd.QUEUED)

    # The drain keeps running every minute; investigations finish.
    for minute in range(1, 180):
        store.clock = T0 + timedelta(minutes=15 + minute)
        _finish_old_rcas(store)
        rd.drain_with_store(store)

    assert store.by_status(rd.QUEUED) == []
    assert store.by_status("error") == []
    assert all(store.incidents[i]["dispatched_at"] is not None for i in ids)
    # One RCA per incident, none twice.
    assert sorted(c.incident_id for c in started) == ids


def test_the_burst_never_runs_more_than_the_concurrency_cap(store, started):
    peak = 0
    for n in range(34):
        store.clock = T0 + timedelta(seconds=n * 26)
        _alert(store, f"inc-{n:02d}")
        rd.drain_with_store(store)
        peak = max(peak, store.usage().in_flight)
    for minute in range(1, 180):
        store.clock = T0 + timedelta(minutes=15 + minute)
        _finish_old_rcas(store, minutes=20)
        rd.drain_with_store(store)
        peak = max(peak, store.usage().in_flight)
    assert peak <= rd.MAX_CONCURRENT


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


def test_a_new_alert_takes_its_turn_behind_alerts_already_waiting(store, started):
    for n in range(rd.MAX_PER_WINDOW + 1):
        _alert(store, f"inc-{n}")
    store.clock = T0 + timedelta(minutes=6)  # the window reopened
    assert _alert(store, "late") == rd.QUEUED_OUTCOME
    assert store.incidents["late"]["reason"] == rd.REASON_WAITING


def test_the_queue_drains_most_severe_first_then_oldest(store, started):
    _fill_window_then_queue(store)
    for incident_id, severity, minute in [
        ("low-old", "low", 1), ("high-new", "high", 4), ("crit", "critical", 5), ("high-old", "high", 2),
    ]:
        store.add(incident_id, severity=severity)
        store.queue(incident_id, rd.REASON_RATE, _request(incident_id))
        store.incidents[incident_id]["queued_at"] = T0 + timedelta(minutes=minute)

    rd.drain_with_store(store)

    assert [c.incident_id for c in started[rd.MAX_PER_WINDOW:]] == ["crit", "high-old", "high-new", "low-old"]


def test_each_queued_incident_runs_its_own_prompt(store, started):
    _fill_window_then_queue(store, "a", "b")
    rd.drain_with_store(store)
    assert {c.incident_id: c.prompt for c in started[rd.MAX_PER_WINDOW:]} == {
        "a": "Investigate a", "b": "Investigate b",
    }


def test_a_drain_over_budget_says_why_the_alerts_still_wait(store, started):
    for n in range(rd.MAX_PER_WINDOW + 2):
        _alert(store, f"inc-{n}")
    store.clock = T0 + timedelta(minutes=10)  # the window reopened, but 5 are still running
    for n in range(rd.MAX_CONCURRENT - rd.MAX_PER_WINDOW):
        store.add(f"extra-{n}")
        store.incidents[f"extra-{n}"].update(aurora_status="running", dispatched_at=T0)

    assert rd.drain_with_store(store)["dispatched"] == 0
    assert store.incidents[f"inc-{rd.MAX_PER_WINDOW}"]["reason"] == rd.REASON_CONCURRENCY


def test_a_failed_start_is_queued_for_retry(monkeypatch, store, started):
    _fill_window_then_queue(store, "a")
    monkeypatch.setattr(rd, "start_rca", MagicMock(side_effect=RuntimeError("broker down")))
    rd.drain_with_store(store)
    assert store.by_status(rd.QUEUED) == ["a"]
    assert store.incidents["a"]["reason"] == rd.REASON_DISPATCH_FAILED


def test_an_incident_that_fails_to_start_does_not_hold_back_the_others(monkeypatch, store, started):
    _fill_window_then_queue(store)
    store.add("broken", severity="critical")
    store.queue("broken", rd.REASON_RATE, _request("broken"))
    store.add("fine", severity="low")
    store.queue("fine", rd.REASON_RATE, _request("fine"))
    ok = rd.start_rca

    def _broken_one(user_id, incident_id, request, org_id=None):
        if incident_id == "broken":
            raise RuntimeError("broker down")
        return ok(user_id, incident_id, request, org_id=org_id)

    monkeypatch.setattr(rd, "start_rca", _broken_one)
    assert rd.drain_with_store(store)["dispatched"] == 1
    assert store.incidents["fine"]["aurora_status"] == "running"
    assert store.by_status(rd.QUEUED) == ["broken"]


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
    import importlib.util

    import celery_config

    # A fresh copy of the module, loaded with a task decorator that keeps the
    # function body: in CI, Celery is stubbed and @celery_app.task would
    # replace run_prediscovery with a MagicMock.
    def _keep_body(*_a, **_k):
        def deco(fn):
            bound_self = SimpleNamespace(request=SimpleNamespace(id="task-1"))
            return SimpleNamespace(run=lambda *args, **kw: fn(bound_self, *args, **kw))
        return deco

    monkeypatch.setattr(celery_config.celery_app, "task", _keep_body)
    source = importlib.import_module("chat.background.prediscovery_task")
    spec = importlib.util.spec_from_file_location("prediscovery_task_under_test", source.__file__)
    pre = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pre)

    # sys.modules entries, the ones prediscovery's local imports resolve to.
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


# --- Failure handling -----------------------------------------------------------------

def test_a_start_that_is_never_published_releases_its_budget(monkeypatch, store, started):
    monkeypatch.setattr(rd, "start_rca", MagicMock(side_effect=RuntimeError("broker down")))
    assert _alert(store, "inc-1") == rd.QUEUED_OUTCOME
    assert store.incidents["inc-1"]["dispatched_at"] is None
    assert store.usage().started_in_window == 0


def test_a_failed_requeue_reports_failed_and_keeps_the_request_for_the_sweep(monkeypatch, store, started):
    monkeypatch.setattr(rd, "start_rca", MagicMock(side_effect=RuntimeError("broker down")))
    monkeypatch.setattr(store, "queue", MagicMock(side_effect=RuntimeError("db down")))
    assert _alert(store, "inc-1") == rd.FAILED
    assert store.incidents["inc-1"]["request"] is not None


def test_a_published_start_clears_the_stored_request(store, started):
    assert _alert(store, "inc-1") == rd.DISPATCHED
    assert store.incidents["inc-1"]["request"] is None
    assert store.incidents["inc-1"]["task"] == "task-inc-1"


def test_an_org_at_capacity_is_not_read_into_memory(store, started):
    _fill_window_then_queue(store, "a", "b")
    store.add("x")
    store.incidents["x"].update(aurora_status="running", dispatched_at=store.clock)
    for n in range(rd.MAX_CONCURRENT - 1):
        store.add(f"run-{n}")
        store.incidents[f"run-{n}"]["aurora_status"] = "running"
    reads = store.queue_reads
    assert rd.drain_with_store(store)["dispatched"] == 0
    assert store.queue_reads == reads
    assert store.incidents["a"]["reason"] == rd.REASON_CONCURRENCY


def test_a_running_chat_triggered_rca_counts_as_in_flight(store):
    # Not an alert dispatch (no rca_dispatched_at), but it costs the same.
    store.add("chat-rca")
    store.incidents["chat-rca"]["aurora_status"] = "running"
    assert store.usage() == rd.BudgetUsage(0, 0, 1)


def test_the_sql_budget_counts_running_rcas_without_a_dispatch_timestamp():
    cursor = MagicMock()
    cursor.fetchone.return_value = (0, 0, 0)
    rd.SqlStore(cursor, MagicMock(), ORG).usage()
    sql = " ".join(cursor.execute.call_args[0][0].split())
    # In flight: any running RCA on an incident, whatever started it...
    assert "(aurora_status IN ('running', 'summarizing') AND updated_at >" in sql
    assert "OR aurora_status IN ('running', 'summarizing'))" in sql
    # ...while the rate counts only alert dispatches.
    assert "COUNT(*) FILTER (WHERE rca_dispatched_at > LOCALTIMESTAMP - make_interval(secs => %s))" in sql


def test_the_sql_drain_takes_the_most_severe_then_oldest_incident():
    cursor = MagicMock()
    cursor.fetchone.return_value = None
    rd.SqlStore(cursor, MagicMock(), ORG).next_queued(["skip-me"])
    sql, params = cursor.execute.call_args[0]
    sql = " ".join(sql.split())
    assert "WHEN 'critical' THEN 0" in sql
    assert "END, rca_queued_at, id LIMIT 1 FOR UPDATE SKIP LOCKED" in sql
    assert params == (ORG, rd.QUEUED, ["skip-me"])


def test_a_refused_publish_fails_the_session_it_created(monkeypatch):
    import importlib

    task_mod = importlib.import_module("chat.background.task")
    failed = []
    monkeypatch.setattr(task_mod, "create_background_chat_session", lambda **_k: "sess-1")
    monkeypatch.setattr(task_mod, "run_background_chat",
                        SimpleNamespace(delay=MagicMock(side_effect=RuntimeError("broker down"))))
    monkeypatch.setattr(task_mod, "_update_session_status",
                        lambda sid, status, user_id: failed.append((sid, status)))
    with pytest.raises(RuntimeError):
        rd.start_rca(USER, "inc-1", _request("inc-1"))
    assert failed == [("sess-1", "failed")]


def test_every_start_carries_the_org_the_incident_was_queued_in(store, started):
    _fill_window_then_queue(store, "a")
    rd.drain_with_store(store)
    assert {c.org_id for c in started} == {ORG}


def test_the_session_is_created_in_the_queue_org_so_a_moved_owner_is_refused(monkeypatch):
    import importlib

    task_mod = importlib.import_module("chat.background.task")
    sessions = []
    monkeypatch.setattr(task_mod, "create_background_chat_session",
                        lambda **kw: sessions.append(kw) or "sess-1")
    monkeypatch.setattr(task_mod, "run_background_chat",
                        SimpleNamespace(delay=MagicMock(return_value=SimpleNamespace(id="task-1"))))
    assert rd.start_rca(USER, "inc-1", _request("inc-1"), org_id=ORG) == "task-1"
    assert sessions[0]["org_id"] == ORG


def test_a_temporary_error_is_reported_as_failed_not_missing(monkeypatch):
    pool = MagicMock()
    pool.get_admin_connection.side_effect = RuntimeError("pool exhausted")
    monkeypatch.setattr(rd, "db_pool", pool)
    outcome = rd.dispatch_alert_rca(
        user_id=USER, incident_id="inc-1", source="sentry", prompt="p", rail_text="r",
    )
    assert outcome == rd.FAILED


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
        if self._last.startswith("SELECT LOCALTIMESTAMP"):
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


NEVER_STARTED = "WHERE i.status = 'investigating' AND i.updated_at < %s"


def test_the_never_started_sweep_skips_queued_incidents(monkeypatch):
    cursor, reported = _sweep(monkeypatch, {
        # Even if a queued row were returned, it is not failed.
        NEVER_STARTED: [("queued-inc", "queued", None, None)],
    })
    sql = next(s for s, _ in cursor.executed if NEVER_STARTED in s)
    assert "COALESCE(i.aurora_status, 'idle') != 'queued'" in sql
    assert _errored(cursor) == []
    assert reported == []


def test_the_never_started_sweep_still_fails_a_dispatched_rca_that_was_lost(monkeypatch):
    cursor, reported = _sweep(monkeypatch, {
        NEVER_STARTED: [("lost-inc", "idle", None, None)],
        "UPDATE incidents SET aurora_status = 'error'": [("lost-inc",)],
    })
    assert _errored(cursor) == ["lost-inc"]
    assert reported == ["lost-inc"]


def test_the_sweep_requeues_a_reserved_start_that_was_never_published(monkeypatch):
    # The dispatcher reserved it, then its worker died before publishing.
    cursor, reported = _sweep(monkeypatch, {
        NEVER_STARTED: [("reserved-inc", "idle", None, None, True)],
    })
    assert _errored(cursor) == []
    assert reported == []
    requeue = next(e for e in cursor.executed if "rca_queue_reason = 'dispatch_failed'" in e[0])
    assert requeue[1][0] == "reserved-inc"


def _update_for(cursor, fragment):
    return next(e for e in cursor.executed if e[0].startswith("UPDATE incidents") and fragment in e[0])


@pytest.mark.parametrize("cur_aurora, fragment", [
    ("idle", "SET aurora_status = 'error'"),
    ("complete", "SET status = 'analyzed'"),
])
def test_the_never_started_cleanup_rechecks_that_the_incident_was_not_queued_since(monkeypatch, cur_aurora, fragment):
    # Dispatch may queue the incident between the sweep's SELECT and its UPDATE.
    cursor, _ = _sweep(monkeypatch, {NEVER_STARTED: [("inc-1", cur_aurora, None, None)]})
    sql, params = _update_for(cursor, fragment)
    assert "AND COALESCE(aurora_status, 'idle') != 'queued'" in sql
    assert params == ("inc-1",)


def test_the_reservation_recovery_leaves_a_renewed_reservation_alone(monkeypatch):
    cursor, _ = _sweep(monkeypatch, {NEVER_STARTED: [("reserved-inc", "idle", None, None, True)]})
    sql, params = _update_for(cursor, "rca_queue_reason = 'dispatch_failed'")
    # A renewal moves updated_at past the threshold, so it no longer matches.
    assert "AND updated_at < %s" in sql
    assert "AND COALESCE(aurora_status, 'idle') = 'idle'" in sql
    assert "AND rca_run_task_id IS NULL AND rca_queued_request IS NOT NULL" in sql
    assert params == ("reserved-inc", DB_NOW - timedelta(minutes=20))
