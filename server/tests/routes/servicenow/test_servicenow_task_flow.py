"""process_servicenow_event: connection scope and the reopen-without-RCA path."""

import importlib
import json
import os
import sys
import uuid
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _celery_stub() -> ModuleType:
    """A celery_config whose @celery_app.task returns the plain function (with a no-op .delay)."""
    celery_cfg = ModuleType("celery_config")

    class _App:
        def task(self, *dargs, **dkwargs):
            def _decorator(fn):
                fn.delay = lambda *a, **k: None  # type: ignore[attr-defined]
                return fn
            if dargs and callable(dargs[0]) and not dkwargs:
                return dargs[0]
            return _decorator

    celery_cfg.celery_app = _App()  # type: ignore[attr-defined]
    return celery_cfg


def _prompt_builder_stub() -> ModuleType:
    rca_pb = ModuleType("chat.background.rca_prompt_builder")
    rca_pb.build_rca_prompt = lambda *a, **k: ("prompt", "rail")  # type: ignore[attr-defined]
    return rca_pb


@pytest.fixture
def tasks(monkeypatch):
    """A fresh routes.servicenow.tasks imported under the stub celery_config.

    CI installs no celery, and tests/auth leaves a MagicMock celery_config in
    sys.modules; a module-level import would then turn process_servicenow_event
    into a MagicMock that silently does nothing. Importing inside the fixture
    with our stub pinned makes the task a plain function every time.
    """
    monkeypatch.setitem(sys.modules, "celery_config", _celery_stub())
    monkeypatch.setitem(sys.modules, "chat.background.rca_prompt_builder", _prompt_builder_stub())
    monkeypatch.delitem(sys.modules, "routes.servicenow.tasks", raising=False)
    mod = importlib.import_module("routes.servicenow.tasks")
    assert not isinstance(mod.process_servicenow_event, MagicMock)
    return mod


SYS_ID = "d" * 32
CREDS = {"instance_url": "https://acme.service-now.com", "auth_type": "basic", "username": "u", "password": "p"}


def _payload(**fields):
    body = {"sys_id": SYS_ID, "number": "INC0010009", "event": "inserted", "priority": "2",
            "state": "1", "short_description": "Checkout down"}
    body.update(fields)
    return body


class _Cursor:
    def __init__(self, fetch=None, fetches=None):
        self.fetch = fetch
        self.fetches = list(fetches) if fetches else None  # consumed in order when given
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        if self.fetches:
            return self.fetches.pop(0)
        return self.fetch

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _Pool:
    """Counts how many pooled connections are checked out at any moment."""

    def __init__(self):
        self.open = 0
        self.enters = 0

    def get_admin_connection(self):
        pool = self

        class _Ctx:
            def __enter__(self):
                pool.open += 1
                pool.enters += 1
                return _Conn(_Cursor())

            def __exit__(self, *exc):
                pool.open -= 1
                return False

        return _Ctx()


def _run_task(tasks, payload, decision):
    return tasks.process_servicenow_event(None, payload, "user-1", decision=decision)  # plain function under the stub


@pytest.fixture
def pool(monkeypatch):
    """Stub the three modules the task imports inside its body.

    They go in via sys.modules, not setattr on the real modules: tests/auth
    leaves MagicMock stand-ins for ``utils.auth.*`` / ``utils.db.*`` in
    sys.modules in a full-suite run, and a setattr on those would silently
    miss, making the task return early at the org lookup.
    """
    fake = _Pool()
    pool_mod = ModuleType("utils.db.connection_pool")
    pool_mod.db_pool = fake  # type: ignore[attr-defined]
    auth_mod = ModuleType("utils.auth.stateless_auth")
    auth_mod.set_rls_context = lambda *a, **k: "org-1"  # type: ignore[attr-defined]
    token_mod = ModuleType("utils.auth.token_management")
    token_mod.get_token_data = lambda user_id, source: dict(CREDS)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "utils.db.connection_pool", pool_mod)
    monkeypatch.setitem(sys.modules, "utils.auth.stateless_auth", auth_mod)
    monkeypatch.setitem(sys.modules, "utils.auth.token_management", token_mod)
    return fake


def test_new_incident_decisions_run_with_no_pooled_connection_held(monkeypatch, tasks, pool):
    seen = {}
    monkeypatch.setattr(tasks, "_find_existing", lambda *a: None)

    def query_check(creds, sys_id, clauses):
        seen["query_open"] = pool.open
        assert clauses == ["priority=1"]
        return True

    def create(cursor, conn, user_id, org_id, *rest):
        seen["create_open"] = pool.open
        assert (user_id, org_id) == ("user-1", "org-1")
        return True

    monkeypatch.setattr(tasks, "_saved_query_matches", query_check)
    monkeypatch.setattr(tasks, "_create_and_investigate", create)

    _run_task(tasks, _payload(), {"action": "query", "query": ["priority=1"]})

    assert seen["query_open"] == 0, "the saved-query check must not hold a pooled connection"
    assert seen["create_open"] == 1, "the writes run on a fresh connection"
    assert pool.enters == 2
    assert pool.open == 0


def test_judge_mode_decides_outside_the_connection_and_creates_inside_it(monkeypatch, tasks, pool):
    seen = {}
    monkeypatch.setattr(tasks, "_find_existing", lambda *a: None)
    monkeypatch.setattr(tasks, "_create_and_investigate", lambda *a: seen.setdefault("create_open", pool.open) or True)

    def judge(payload, user_id, *, start_rca):
        seen["judge_open"] = pool.open
        return {"status": "judged", "started": start_rca()}

    judge_mod = ModuleType("routes.servicenow.rca_judge")
    judge_mod.judge_and_route = judge  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "routes.servicenow.rca_judge", judge_mod)

    _run_task(tasks, _payload(), {"action": "judge"})

    assert seen["judge_open"] == 0
    assert seen["create_open"] == 1
    assert pool.open == 0


def test_known_incident_is_synced_with_the_payload_for_a_possible_reopen(monkeypatch, tasks, pool):
    existing = {"id": "inc-1", "status": "resolved", "rca_task_id": None}
    synced = MagicMock()
    monkeypatch.setattr(tasks, "_find_existing", lambda *a: dict(existing))
    monkeypatch.setattr(tasks, "_store_event", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "_sync_existing", synced)
    monkeypatch.setattr(tasks, "_notify_sse", lambda *a: None)
    create = MagicMock()
    monkeypatch.setattr(tasks, "_create_and_investigate", create)

    _run_task(tasks, _payload(event="updated", state="2"), {"action": "rca"})

    synced.assert_called_once()
    kwargs = synced.call_args.kwargs
    assert kwargs["sys_id"] == SYS_ID
    assert kwargs["payload"]["number"] == "INC0010009"
    assert kwargs["title"]
    create.assert_not_called()
    assert pool.enters == 1
    assert pool.open == 0


@pytest.mark.parametrize("existing_status, new_status, rca_task_id, dispatched", [
    ("resolved", "investigating", None, True),      # arrived resolved, reopened, never investigated
    ("resolved", "investigating", "task-9", False),  # reopened, but an RCA already ran
    ("resolved", "resolved", None, False),           # still resolved: nothing to do
    ("investigating", "investigating", None, False),  # plain update of an active incident
])
def test_reopen_dispatches_an_rca_only_when_none_ever_ran(monkeypatch, tasks, existing_status, new_status, rca_task_id, dispatched):
    # First fetch: the row lock (status, rca task); second: UPDATE ... RETURNING status.
    cursor = _Cursor(fetches=[(existing_status, rca_task_id), (new_status,)])
    conn = _Conn(cursor)
    monkeypatch.setattr(tasks, "_record_lifecycle_event", lambda *a, **k: None)
    dispatch = MagicMock()
    monkeypatch.setattr(tasks, "_dispatch_rca", dispatch)
    existing = {"id": "inc-1", "status": existing_status, "rca_task_id": rca_task_id}

    tasks._sync_existing(
        cursor, conn, "user-1", "org-1", existing, new_status, "high", {"stateValue": "2"},
        "INC0010009", "updated", title="Checkout down", sys_id=SYS_ID, payload=_payload(),
    )

    assert "FOR NO KEY UPDATE" in cursor.executed[0][0]
    if dispatched:
        dispatch.assert_called_once()
        args = dispatch.call_args.args
        assert args[3] == "inc-1"
        assert args[4] == "Checkout down"
        assert args[6] == SYS_ID
    else:
        dispatch.assert_not_called()


def test_reopen_trusts_the_locked_row_over_the_earlier_read(monkeypatch, tasks):
    """A concurrent delivery already reopened the incident: the lock shows it active, so no second RCA."""
    cursor = _Cursor(fetches=[("investigating", None), ("investigating",)])
    monkeypatch.setattr(tasks, "_record_lifecycle_event", lambda *a, **k: None)
    dispatch = MagicMock()
    monkeypatch.setattr(tasks, "_dispatch_rca", dispatch)
    existing = {"id": "inc-1", "status": "resolved", "rca_task_id": None}  # stale: read before the other task committed

    tasks._sync_existing(
        cursor, _Conn(cursor), "user-1", "org-1", existing, "investigating", "high", {"stateValue": "2"},
        "INC0010009", "updated", title="Checkout down", sys_id=SYS_ID, payload=_payload(),
    )

    dispatch.assert_not_called()


def test_find_existing_reports_whether_an_rca_ran(tasks):
    cursor = _Cursor(fetch=("inc-1", "resolved", None))
    assert tasks._find_existing(cursor, "org-1", "user-1", 42) == {"id": "inc-1", "status": "resolved", "rca_task_id": None}
    cursor = _Cursor(fetch=("inc-1", "analyzed", "task-1"))
    assert tasks._find_existing(cursor, "org-1", "user-1", 42)["rca_task_id"] == "task-1"
    assert tasks._find_existing(_Cursor(fetch=None), "org-1", "user-1", 42) is None


# ── dispatch failures are retryable ──────────────────────────────────


def _sync(tasks, cursor, conn, existing_status="resolved", **kwargs):
    existing = {"id": "inc-1", "status": existing_status, "rca_task_id": None}
    return tasks._sync_existing(
        cursor, conn, "user-1", "org-1", existing, "investigating", "high", {"stateValue": "2"},
        "INC0010009", "updated", title="Checkout down", sys_id=SYS_ID, payload=_payload(), **kwargs,
    )


def test_reopen_commits_the_new_state_only_once_the_rca_is_dispatched(monkeypatch, tasks):
    cursor = _Cursor(fetches=[("resolved", None), ("investigating",)])
    conn = _Conn(cursor)
    lifecycle = MagicMock()
    monkeypatch.setattr(tasks, "_record_lifecycle_event", lifecycle)
    commits_at_dispatch = []
    monkeypatch.setattr(tasks, "_dispatch_rca", lambda *a: commits_at_dispatch.append(conn.commits))

    _sync(tasks, cursor, conn)

    assert commits_at_dispatch == [0]  # the status change was still pending when the RCA was queued
    assert conn.commits == 1
    lifecycle.assert_called_once()
    assert lifecycle.call_args.args[5:] == ("status_changed", "investigating", "resolved")


def test_reopen_dispatch_failure_leaves_nothing_committed_and_propagates(monkeypatch, tasks):
    cursor = _Cursor(fetches=[("resolved", None), ("investigating",)])
    conn = _Conn(cursor)
    lifecycle = MagicMock()
    monkeypatch.setattr(tasks, "_record_lifecycle_event", lifecycle)
    monkeypatch.setattr(tasks, "_dispatch_rca", MagicMock(side_effect=tasks.RcaDispatchError("broker down")))

    with pytest.raises(tasks.RcaDispatchError):
        _sync(tasks, cursor, conn)

    assert conn.commits == 0
    lifecycle.assert_not_called()


@pytest.mark.parametrize("old_task, retrying, dispatched", [
    (None, True, True),       # the attempt being retried failed to queue this incident's RCA
    (None, False, False),     # a fresh delivery never starts one for an active incident
    ("task-9", True, False),  # an RCA is already recorded
])
def test_a_retried_delivery_starts_the_rca_an_active_incident_never_got(monkeypatch, tasks, old_task, retrying, dispatched):
    cursor = _Cursor(fetches=[("investigating", old_task), ("investigating",)])
    conn = _Conn(cursor)
    monkeypatch.setattr(tasks, "_record_lifecycle_event", lambda *a, **k: None)
    dispatch = MagicMock()
    monkeypatch.setattr(tasks, "_dispatch_rca", dispatch)

    _sync(tasks, cursor, conn, existing_status="investigating", retrying=retrying)

    assert dispatch.called == dispatched
    assert conn.commits == 1


class _BoundTask:
    """What Celery passes as ``self``: ``request.retries`` counts the earlier attempts."""

    def __init__(self, retries):
        self.request = SimpleNamespace(retries=retries)

    def retry(self, exc=None):
        return RuntimeError(f"retry after {exc!r}")


@pytest.mark.parametrize("task_self, retrying", [(None, False), (_BoundTask(0), False), (_BoundTask(2), True)])
def test_task_tells_the_sync_whether_this_delivery_is_a_retry(monkeypatch, tasks, pool, task_self, retrying):
    synced = MagicMock()
    monkeypatch.setattr(tasks, "_find_existing", lambda *a: {"id": "inc-1", "status": "investigating", "rca_task_id": None})
    monkeypatch.setattr(tasks, "_store_event", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "_sync_existing", synced)
    monkeypatch.setattr(tasks, "_notify_sse", lambda *a: None)

    tasks.process_servicenow_event(task_self, _payload(event="updated", state="2"), "user-1", decision={"action": "rca"})

    assert synced.call_args.kwargs["retrying"] is retrying


def test_a_failed_dispatch_goes_through_the_celery_retry(monkeypatch, tasks, pool):
    monkeypatch.setattr(tasks, "_find_existing", lambda *a: {"id": "inc-1", "status": "resolved", "rca_task_id": None})
    monkeypatch.setattr(tasks, "_store_event", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "_sync_existing", MagicMock(side_effect=tasks.RcaDispatchError("broker down")))
    notify = MagicMock()
    monkeypatch.setattr(tasks, "_notify_sse", notify)

    with pytest.raises(RuntimeError, match="retry after"):
        tasks.process_servicenow_event(_BoundTask(0), _payload(event="updated", state="2"), "user-1", decision={"action": "rca"})

    notify.assert_not_called()
    assert pool.open == 0


def _background_stub(delay):
    mod = ModuleType("chat.background.task")
    mod.is_background_chat_allowed = lambda user_id: True  # type: ignore[attr-defined]
    mod.create_background_chat_session = lambda **kw: "sess-1"  # type: ignore[attr-defined]
    mod.run_background_chat = SimpleNamespace(delay=delay)  # type: ignore[attr-defined]
    return mod


def _dispatch(tasks, cursor, conn):
    return tasks._dispatch_rca(cursor, conn, "user-1", "inc-1", "Checkout down", "INC0010009", SYS_ID, _payload())


def test_dispatch_records_the_task_id_and_commits(monkeypatch, tasks):
    monkeypatch.setitem(sys.modules, "chat.background.task", _background_stub(lambda **kw: SimpleNamespace(id="task-7")))
    cursor = _Cursor()
    conn = _Conn(cursor)

    _dispatch(tasks, cursor, conn)

    sql, params = cursor.executed[-1]
    assert "rca_celery_task_id" in sql
    assert params == ("task-7", "inc-1")
    assert conn.commits == 1
    assert conn.rollbacks == 0


def test_dispatch_failure_rolls_back_marks_the_session_failed_and_raises(monkeypatch, tasks):
    def broker_down(**kw):
        raise ConnectionError("broker down")

    monkeypatch.setitem(sys.modules, "chat.background.task", _background_stub(broker_down))
    cursor = _Cursor()
    conn = _Conn(cursor)

    with pytest.raises(tasks.RcaDispatchError):
        _dispatch(tasks, cursor, conn)

    assert conn.rollbacks == 1
    assert not any("rca_celery_task_id" in sql for sql, _ in cursor.executed)
    sql, params = cursor.executed[-1]
    assert "chat_sessions" in sql
    assert params == ("failed", "sess-1")
    assert conn.commits == 1  # only the failed-session mark, after the rollback


def test_dispatch_skips_a_rate_limited_user_without_touching_the_connection(monkeypatch, tasks):
    stub = _background_stub(lambda **kw: SimpleNamespace(id="task-7"))
    stub.is_background_chat_allowed = lambda user_id: False  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "chat.background.task", stub)
    cursor = _Cursor()
    conn = _Conn(cursor)

    _dispatch(tasks, cursor, conn)

    assert cursor.executed == []
    assert conn.commits == 0


# ── the reopen lock against a real Postgres ───────────────────────────
#
# _sync_existing keeps the incident row locked while _dispatch_rca creates the
# RCA chat session on a second connection. chat_sessions.incident_id references
# incidents(id), so that insert takes a KEY SHARE lock on the incident row; with
# the wrong lock mode it waits on the task's own transaction and never returns.
# Mocks cannot see that, so this runs against Postgres and skips without one
# (POSTGRES_HOST / POSTGRES_PORT, like the other db tests).


def _pg_connect():
    psycopg2 = pytest.importorskip("psycopg2")
    host, port = os.getenv("POSTGRES_HOST"), os.getenv("POSTGRES_PORT")
    if not host or not port:
        pytest.skip("PostgreSQL is not reachable (POSTGRES_HOST/PORT unset or unreachable)")
    try:
        return psycopg2.connect(
            host=host, port=int(port), dbname=os.getenv("POSTGRES_DB", "aurora_db"),
            user=os.getenv("POSTGRES_USER", "aurora"), password=os.getenv("POSTGRES_PASSWORD", ""),
            sslmode=os.getenv("POSTGRES_SSLMODE", "prefer"),
        )
    except psycopg2.OperationalError:
        pytest.skip("PostgreSQL is not reachable (POSTGRES_HOST/PORT unset or unreachable)")


@pytest.fixture
def pg_schema():
    """Two connections on a throwaway schema with the incidents -> chat_sessions foreign key."""
    task_conn = _pg_connect()
    other_conn = _pg_connect()
    schema = f"snlock_{uuid.uuid4().hex[:12]}"
    try:
        with task_conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {schema}")
            cur.execute(f"SET search_path TO {schema}")
            cur.execute(
                """CREATE TABLE incidents (
                       id uuid PRIMARY KEY, status text NOT NULL, rca_celery_task_id text,
                       alert_metadata jsonb, severity text, resolved_at timestamptz,
                       updated_at timestamptz, aurora_summary text)"""
            )
            cur.execute(
                """CREATE TABLE chat_sessions (
                       id uuid PRIMARY KEY, status text,
                       incident_id uuid REFERENCES incidents(id) ON DELETE CASCADE)"""
            )
        task_conn.commit()
        with other_conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")
            cur.execute("SET lock_timeout = '2000ms'")  # a blocked insert fails fast instead of hanging the test
        other_conn.commit()
        yield task_conn, other_conn
    finally:
        for conn in (task_conn, other_conn):
            try:
                conn.rollback()
            except Exception:
                pass
        try:
            with task_conn.cursor() as cur:
                cur.execute(f"DROP SCHEMA {schema} CASCADE")
            task_conn.commit()
        finally:
            task_conn.close()
            other_conn.close()


def _insert_resolved_incident(conn):
    incident_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO incidents (id, status, alert_metadata, severity) VALUES (%s, 'resolved', %s, 'high')",
            (incident_id, json.dumps({"stateValue": "6"})),
        )
    conn.commit()
    return incident_id


@pytest.mark.parametrize("lock_mode, insert_blocks", [("FOR UPDATE", True), ("FOR NO KEY UPDATE", False)])
def test_only_a_no_key_update_lock_lets_the_session_insert_through(pg_schema, lock_mode, insert_blocks):
    """The lock-mode fact the reopen path relies on, stated against Postgres itself."""
    psycopg2 = pytest.importorskip("psycopg2")
    task_conn, other_conn = pg_schema
    incident_id = _insert_resolved_incident(task_conn)

    with task_conn.cursor() as cur:
        cur.execute(f"SELECT status FROM incidents WHERE id = %s {lock_mode}", (incident_id,))

    def insert_session():
        with other_conn.cursor() as cur:
            cur.execute("INSERT INTO chat_sessions (id, status, incident_id) VALUES (%s, 'in_progress', %s)",
                        (str(uuid.uuid4()), incident_id))
        other_conn.commit()

    if insert_blocks:
        with pytest.raises(psycopg2.errors.LockNotAvailable):
            insert_session()
    else:
        insert_session()
    task_conn.rollback()


def test_reopen_dispatch_creates_the_session_on_another_connection_without_deadlocking(monkeypatch, tasks, pg_schema):
    """The real _sync_existing and _dispatch_rca SQL, with the session insert on a second connection."""
    task_conn, other_conn = pg_schema
    incident_id = _insert_resolved_incident(task_conn)
    monkeypatch.setattr(tasks, "_record_lifecycle_event", lambda *a, **k: None)

    def create_session(**kw):
        # What chat.background.task.create_background_chat_session does: its own
        # connection, an insert whose foreign key points at the locked incident.
        session_id = str(uuid.uuid4())
        with other_conn.cursor() as cur:
            cur.execute("INSERT INTO chat_sessions (id, status, incident_id) VALUES (%s, 'in_progress', %s)",
                        (session_id, kw["incident_id"]))
        other_conn.commit()
        return session_id

    stub = _background_stub(lambda **kw: SimpleNamespace(id="task-pg"))
    stub.create_background_chat_session = create_session  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "chat.background.task", stub)

    with task_conn.cursor() as cursor:
        tasks._sync_existing(
            cursor, task_conn, "user-1", "org-1",
            {"id": incident_id, "status": "resolved", "rca_task_id": None},
            "investigating", "high", {"stateValue": "2"}, "INC0010009", "updated",
            title="Checkout down", sys_id=SYS_ID, payload=_payload(),
        )

    with other_conn.cursor() as cur:  # what every other connection sees once the sync returns
        cur.execute("SELECT status, rca_celery_task_id FROM incidents WHERE id = %s", (incident_id,))
        status, task_id = cur.fetchone()
        cur.execute("SELECT count(*) FROM chat_sessions WHERE incident_id = %s", (incident_id,))
        sessions = cur.fetchone()[0]
    assert (status, task_id, sessions) == ("investigating", "task-pg", 1)
