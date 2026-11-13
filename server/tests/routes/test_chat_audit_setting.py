"""GET/PUT /api/org/chat-audit: who can audit conversations."""

import pytest
from flask import Flask

ROLES = {("admin-1", "org-1"): "admin", ("editor-1", "org-1"): "editor", ("viewer-1", "org-1"): "viewer"}
GRANTS = {"admin": {("admin", "access"), ("chat", "read")}, "editor": {("chat", "read")},
          "viewer": {("chat", "read")}}


@pytest.fixture
def call(monkeypatch):
    import routes.connector_access as mod

    saved = []
    monkeypatch.setattr("utils.auth.viewer_run.save_editor_audit", lambda org, user, editors: saved.append(
        (org, user, editors)))
    monkeypatch.setattr("utils.auth.viewer_run.org_allows_editor_audit", lambda org: True)
    monkeypatch.setattr("utils.auth.viewer_run.may_audit_chats", lambda user, org: ROLES.get((user, org)) == "admin")

    def _run(handler, user, method="GET", body=None):
        rbac_globals = getattr(mod, handler).__globals__
        for name, value in {
            "get_user_id_from_request": lambda: user,
            "get_org_id_from_request": lambda: "org-1",
            "_audit_auth_failure": lambda *a, **k: None,
            "enforce_with_reload": lambda u, o, res, act: (res, act) in GRANTS.get(ROLES.get((u, o)), set()),
        }.items():
            monkeypatch.setitem(rbac_globals, name, value)
        monkeypatch.setattr(mod, "get_org_id_from_request", lambda: "org-1")
        app = Flask(__name__)
        with app.test_request_context("/api/org/chat-audit", method=method, json=body):
            rv = getattr(mod, handler)()
            resp, status = rv if isinstance(rv, tuple) else (rv, rv.status_code)
        return resp.get_json(), status

    return _run, saved


def test_get_reports_the_setting_and_whether_the_caller_may_audit(call):
    run, _ = call
    assert run("get_chat_audit", "admin-1") == ({"editors": True, "allowed": True}, 200)
    assert run("get_chat_audit", "viewer-1") == ({"editors": True, "allowed": False}, 200)


def test_only_admins_can_change_it(call):
    run, saved = call
    _, status = run("put_chat_audit", "editor-1", "PUT", {"editors": True})
    assert status == 403 and saved == []
    body, status = run("put_chat_audit", "admin-1", "PUT", {"editors": True})
    assert status == 200 and body == {"editors": True} and saved == [("org-1", "admin-1", True)]


@pytest.mark.parametrize("body", [{}, {"editors": "yes"}, {"editors": 1}, None])
def test_bad_values_are_400(call, body):
    run, saved = call
    resp, status = run("put_chat_audit", "admin-1", "PUT", body)
    assert status == 400 and saved == []
