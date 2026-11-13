"""Opening a chat by id: the owner, someone allowed to audit chats, or the
incident's / action's readers. Anyone else gets 404, the same as an unknown
id, so ids can't be probed.
"""

from contextlib import contextmanager
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from flask import Flask

OWNER, OTHER = "uid-owner", "uid-other"


@pytest.fixture
def access(monkeypatch):
    """Who may do what: the audit rule and RBAC grants, set per test."""
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")  # config.rate_limiting needs one
    import routes.chat_routes as chat_routes

    state = {"auditor": False, "grants": {("incidents", "read"), ("actions", "read")}, "checked": []}

    def enforce(user_id, org_id, resource, action):
        state["checked"].append((resource, action))
        return (resource, action) in state["grants"]

    monkeypatch.setattr("utils.auth.viewer_run.may_audit_chats", lambda u, o: state["auditor"])
    monkeypatch.setattr(chat_routes, "may_audit_chats", lambda u, o: state["auditor"])
    monkeypatch.setattr("utils.auth.enforcer.enforce_with_reload", enforce)
    monkeypatch.setattr(chat_routes, "set_rls_context", lambda *a, **k: None)
    monkeypatch.setattr(chat_routes, "get_org_id_from_request", lambda: "org-1")
    state["routes"] = chat_routes
    return state


def _cursor(row):
    cursor = MagicMock()
    cursor.fetchone.return_value = row
    cursor.fetchall.return_value = []
    return cursor


def _call(access, monkeypatch, name, row, caller, path="/x"):
    chat_routes = access["routes"]
    cursor = _cursor(row)
    conn = MagicMock()
    conn.cursor.return_value = cursor
    monkeypatch.setattr(chat_routes, "connect_to_db_as_user", lambda: conn)

    @contextmanager
    def admin_connection():
        @contextmanager
        def cur():
            yield cursor
        pooled = MagicMock()
        pooled.cursor = cur
        yield pooled

    pool = MagicMock()
    pool.get_admin_connection = admin_connection
    monkeypatch.setattr(chat_routes, "db_pool", pool)

    handler = getattr(chat_routes, name)
    while hasattr(handler, "__wrapped__"):  # strip the limiter and RBAC wrappers
        handler = handler.__wrapped__
    with Flask(__name__).test_request_context(path):
        if name == "get_chat_sessions":
            response, status = handler(caller)
        else:
            response, status = handler(caller, "sess-1")
    return response.get_json(), status, cursor


def _session_row(owner, *, incident_linked=False, action_run=False):
    now = datetime(2026, 1, 1)
    return ("sess-1", "Title", [{"sender": "user", "text": "hello"}], now, now, {}, "active",
            owner, "inc-1" if incident_linked else None, None, incident_linked, action_run)


ROUTES = {
    # route: the row its query returns for a chat owned by ``owner``
    "get_chat_session": lambda owner, **kw: _session_row(owner, **kw),
    "get_chat_session_status": lambda owner, **kw: ("active", 1, owner, kw.get("incident_linked", False),
                                                     kw.get("action_run", False)),
    "get_chat_messages": lambda owner, **kw: ([{"sender": "user", "text": "hello"}], "active", owner,
                                              kw.get("incident_linked", False), kw.get("action_run", False)),
}


@pytest.mark.parametrize("name", ROUTES)
def test_the_owner_can_open_their_chat(access, monkeypatch, name):
    _, status, cursor = _call(access, monkeypatch, name, ROUTES[name](OWNER), OWNER)
    assert status == 200
    assert "AS incident_linked" in cursor.execute.call_args.args[0]


@pytest.mark.parametrize("name", ROUTES)
def test_another_member_gets_404_like_an_unknown_id(access, monkeypatch, name):
    body, status, _ = _call(access, monkeypatch, name, ROUTES[name](OWNER), OTHER)
    unknown_body, unknown_status, _ = _call(access, monkeypatch, name, None, OTHER)
    assert status == unknown_status == 404 and body == unknown_body


@pytest.mark.parametrize("name", ROUTES)
def test_someone_allowed_to_audit_chats_can_open_it(access, monkeypatch, name):
    access["auditor"] = True
    _, status, _ = _call(access, monkeypatch, name, ROUTES[name](OWNER), OTHER)
    assert status == 200


@pytest.mark.parametrize("name", ROUTES)
def test_an_incident_chat_follows_incidents_read(access, monkeypatch, name):
    row = ROUTES[name](OWNER, incident_linked=True)
    _, status, _ = _call(access, monkeypatch, name, row, OTHER)
    assert status == 200 and ("incidents", "read") in access["checked"]
    access["grants"].discard(("incidents", "read"))
    _, status, _ = _call(access, monkeypatch, name, row, OTHER)
    assert status == 404


@pytest.mark.parametrize("name", ROUTES)
def test_an_action_run_chat_follows_actions_read(access, monkeypatch, name):
    row = ROUTES[name](OWNER, action_run=True)
    _, status, _ = _call(access, monkeypatch, name, row, OTHER)
    assert status == 200
    access["grants"].discard(("actions", "read"))
    _, status, _ = _call(access, monkeypatch, name, row, OTHER)
    assert status == 404


def test_incident_link_covers_the_incident_rca_session():
    from utils.auth.viewer_run import CHAT_SCOPE_SQL

    # The RCA session is reached through incidents.aurora_chat_session_id, which
    # the chat row itself may not point back to.
    assert "cs.incident_id IS NOT NULL" in CHAT_SCOPE_SQL
    assert "i.aurora_chat_session_id::text = cs.id" in CHAT_SCOPE_SQL


def test_a_failed_permission_lookup_fails_closed(access, monkeypatch):
    def boom(*a):
        raise RuntimeError("enforcer down")
    monkeypatch.setattr("utils.auth.enforcer.enforce_with_reload", boom)
    access["auditor"] = True
    row = _session_row(OWNER, incident_linked=True)
    _, status, _ = _call(access, monkeypatch, "get_chat_session", row, OTHER)
    assert status == 404


@pytest.mark.parametrize("auditor, filters_by_user", [(False, True), (True, False)])
def test_org_wide_session_list_is_only_for_auditors(access, monkeypatch, auditor, filters_by_user):
    access["auditor"] = auditor
    _, status, cursor = _call(access, monkeypatch, "get_chat_sessions", None, OTHER, "/sessions?scope=org")
    sql, params = cursor.execute.call_args.args
    assert status == 200 and ("cs.user_id = %s" in sql) is filters_by_user
    assert (OTHER in params) is filters_by_user


def test_the_websocket_refuses_to_continue_someone_elses_chat():
    # Sending into another user's chat would load its history into the agent,
    # which could then repeat it. Incident chats keep their incidents:write rule.
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "main_chatbot.py").read_text()
    assert "SELECT incident_id, user_id FROM chat_sessions WHERE id = %s AND org_id = %s" in source
    assert "if row and not row[0] and row[1] != user_id:" in source
