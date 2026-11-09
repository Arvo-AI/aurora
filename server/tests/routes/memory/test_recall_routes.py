"""GET /search and POST /check-change on the memory blueprint."""

import sys
from unittest.mock import MagicMock

import pytest

ORG_ID = "org-test"
USER_ID = "user-test"
HEADERS = {"X-User-ID": USER_ID, "X-Org-ID": ORG_ID}


@pytest.fixture
def memory_client(monkeypatch):
    pytest.importorskip("flask")
    # Evict so Werkzeug proxies bind to the app this fixture creates; monkeypatch
    # restores the original module objects after the test.
    for mod in [m for m in list(sys.modules) if m.startswith(("routes.", "utils.auth.rbac"))]:
        monkeypatch.delitem(sys.modules, mod)
    for heavy in ("celery_config", "celery", "routes.audit_routes"):
        if heavy not in sys.modules:
            monkeypatch.setitem(sys.modules, heavy, MagicMock())
    monkeypatch.setattr(sys.modules["routes.audit_routes"], "record_audit_event", MagicMock(), raising=False)

    from flask import Flask

    from routes.memory import routes as memory_routes
    from utils.auth import rbac_decorators as rbac

    monkeypatch.setattr(rbac, "get_user_id_from_request", lambda *a, **k: USER_ID)
    monkeypatch.setattr(rbac, "get_org_id_from_request", lambda *a, **k: ORG_ID)
    monkeypatch.setattr(rbac, "enforce_with_reload", lambda *a, **k: True)
    monkeypatch.setattr(rbac, "_audit_auth_failure", lambda *a, **k: None)

    monkeypatch.setattr(memory_routes, "get_org_id_from_request", lambda *a, **k: ORG_ID)
    rls = MagicMock()
    monkeypatch.setattr(memory_routes, "set_rls_context", rls)
    search = MagicMock(return_value=[])
    check = MagicMock(return_value={"status": "NO_MATCH"})
    monkeypatch.setattr(memory_routes, "search_org_memory", search)
    monkeypatch.setattr(memory_routes, "check_change_for_org", check)
    seed = MagicMock(return_value=True)
    monkeypatch.setattr(memory_routes, "seed_platform_memory", seed)

    cursor = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value = cursor
    pool = MagicMock()
    pool.get_user_connection.return_value.__enter__ = lambda s: conn
    pool.get_user_connection.return_value.__exit__ = lambda s, *a: False
    monkeypatch.setattr(memory_routes, "db_pool", pool)

    app = Flask(__name__)  # NOSONAR
    app.register_blueprint(memory_routes.memory_bp)
    client = app.test_client()
    client.cursor = cursor
    client.rls = rls
    client.search = search
    client.check = check
    return client


class _Hit:
    def to_dict(self):
        return {"title": "T", "category": "runbook", "offset": 7}


def test_search_requires_q(memory_client):
    assert memory_client.get("/search", headers=HEADERS).status_code == 400
    assert memory_client.get("/search?q=" + "x" * 2001, headers=HEADERS).status_code == 400
    memory_client.search.assert_not_called()


def test_search_bad_category(memory_client):
    resp = memory_client.get("/search?q=a&category=runbook,nope", headers=HEADERS)
    assert resp.status_code == 400


def test_search_passes_through_and_pins_rls(memory_client):
    memory_client.search.return_value = [_Hit()]
    resp = memory_client.get("/search?q=redis&category=runbook,context&k=3", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.get_json() == {"query": "redis", "hits": [{"title": "T", "category": "runbook", "offset": 7}]}
    args, kwargs = memory_client.search.call_args
    assert args[1:3] == (ORG_ID, "redis")
    assert kwargs == {"categories": ["runbook", "context"], "k": 3}
    assert memory_client.rls.call_args.kwargs["org_id"] == ORG_ID


@pytest.mark.parametrize("raw,expected", [("999", 20), ("0", 1), ("-4", 1), ("abc", 5)])
def test_search_clamps_k(memory_client, raw, expected):
    memory_client.get(f"/search?q=a&k={raw}", headers=HEADERS)
    assert memory_client.search.call_args.kwargs["k"] == expected


def test_search_error_is_generic_500(memory_client):
    memory_client.search.side_effect = RuntimeError("secret detail")
    resp = memory_client.get("/search?q=a", headers=HEADERS)
    assert resp.status_code == 500
    assert "secret" not in resp.get_data(as_text=True)


@pytest.mark.parametrize("body", [{}, {"diff": "  ", "files": [], "description": ""}, None])
def test_check_change_empty_is_400(memory_client, body):
    resp = memory_client.post("/check-change", json=body, headers=HEADERS)
    assert resp.status_code == 400
    memory_client.check.assert_not_called()


def test_check_change_validates_types_and_limits(memory_client):
    from services.memory import change_check

    assert memory_client.post("/check-change", json={"files": "a.py"}, headers=HEADERS).status_code == 400
    assert memory_client.post("/check-change", json={"files": [1]}, headers=HEADERS).status_code == 400
    big = "x" * (change_check.MAX_DIFF_CHARS + 1)
    assert memory_client.post("/check-change", json={"diff": big}, headers=HEADERS).status_code == 413
    many = ["f"] * (change_check.MAX_FILES + 1)
    assert memory_client.post("/check-change", json={"files": many}, headers=HEADERS).status_code == 413


def test_check_change_passes_through_and_pins_rls(memory_client):
    memory_client.check.return_value = {"status": "MATCHES_PAST_INCIDENT", "matches": []}
    resp = memory_client.post(
        "/check-change", json={"diff": "d", "files": ["a.py"], "description": "x"}, headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "MATCHES_PAST_INCIDENT"
    args, kwargs = memory_client.check.call_args
    assert args[1] == ORG_ID
    assert kwargs == {"diff": "d", "files": ["a.py"], "description": "x"}
    assert memory_client.rls.call_args.kwargs["org_id"] == ORG_ID
