"""process_servicenow_event: connection scope and the reopen-without-RCA path."""

import importlib
import sys
from types import ModuleType
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

    def cursor(self):
        return self._cursor

    def commit(self):
        pass


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

    assert "FOR UPDATE" in cursor.executed[0][0]
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
