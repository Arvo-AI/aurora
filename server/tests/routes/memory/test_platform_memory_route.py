"""GET /api/memory/platform/<platform> surfaces a platform's policy memory
(seeding on read); /api/memory/slack stays as an alias with the same body.
``is_protected`` is reported on serialized entries so the client can stop
mirroring PROTECTED_ENTRIES."""

import sys
from datetime import datetime
from unittest.mock import MagicMock

import pytest


ORG_ID = "org-test"
USER_ID = "user-test"
ENTRY_ID = "11111111-1111-1111-1111-111111111111"
HEADERS = {"X-User-ID": USER_ID, "X-Org-ID": ORG_ID}

# id, title, category, description, content, last_edited_by, last_edited_by_name, updated_at
SLACK_ROW = (ENTRY_ID, "Slack", "context", "desc", "policy text", "agent", None, datetime(2024, 1, 1))


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
    monkeypatch.setattr(memory_routes, "set_rls_context", lambda *a, **k: ORG_ID)
    seed = MagicMock(return_value=True)
    monkeypatch.setattr(memory_routes, "seed_platform_memory", seed)

    cursor = MagicMock()
    cursor.fetchone.return_value = SLACK_ROW
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
    client.seed = seed
    return client


def test_platform_route_and_slack_alias_return_the_same_body(memory_client):
    a = memory_client.get("/platform/slack", headers=HEADERS)
    b = memory_client.get("/slack", headers=HEADERS)
    assert a.status_code == b.status_code == 200
    assert a.get_json() == b.get_json()
    entry = a.get_json()["entry"]
    assert entry["id"] == ENTRY_ID
    assert entry["title"] == "Slack"
    assert entry["category"] == "context"
    assert entry["content"] == "policy text"
    assert entry["is_protected"] is True
    memory_client.seed.assert_not_called()  # entry existed; no seed-on-read
    # The lookup is pinned to the spec's identity.
    params = memory_client.cursor.execute.call_args.args[1]
    assert params[1:] == ("context", "Slack")


def test_unknown_platform_is_404_without_touching_the_db(memory_client):
    resp = memory_client.get("/platform/discord", headers=HEADERS)
    assert resp.status_code == 404
    memory_client.cursor.execute.assert_not_called()
    memory_client.seed.assert_not_called()


def test_missing_entry_is_seeded_into_the_request_org_then_reread(memory_client):
    memory_client.cursor.fetchone.side_effect = [None, SLACK_ROW]
    resp = memory_client.get("/platform/slack", headers=HEADERS)
    assert resp.status_code == 200
    memory_client.seed.assert_called_once_with(USER_ID, "slack", org_id=ORG_ID)
    assert resp.get_json()["entry"]["id"] == ENTRY_ID


def test_list_entries_reports_is_protected_only_for_the_slack_pair(memory_client):
    memory_client.cursor.fetchall.return_value = [
        (ENTRY_ID, "Slack", "context", None, "user", "Alex", datetime(2024, 1, 1)),
        ("2", "Slack", "runbook", None, "user", None, None),      # same title, other category
        ("3", "Redis", "context", None, "agent", None, None),
    ]
    resp = memory_client.get("/entries", headers=HEADERS)
    assert resp.status_code == 200
    flags = [(e["category"], e["title"], e["is_protected"]) for e in resp.get_json()["entries"]]
    assert flags == [("context", "Slack", True), ("runbook", "Slack", False), ("context", "Redis", False)]
