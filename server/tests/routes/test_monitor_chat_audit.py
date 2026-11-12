"""Conversation audit routes: who may audit, org isolation, errors."""

from unittest.mock import MagicMock

import pytest
from flask import Flask

# Fake Casbin: who holds which role in which org. Every member may chat; who
# may audit is the org's conversation-audit rule (fake below).
ROLES = {("admin-1", "org-1"): "admin", ("viewer-1", "org-1"): "viewer",
         ("editor-1", "org-1"): "editor", ("admin-2", "org-2"): "admin"}
GRANTS = {"admin": {("admin", "access"), ("chat", "read")}, "editor": {("chat", "read")},
          "viewer": {("chat", "read")}}
EDITORS_MAY_AUDIT = {"org-1": False}


def _fake_may_audit(user_id, org_id):
    role = ROLES.get((user_id, org_id))
    return role == "admin" or (role == "editor" and EDITORS_MAY_AUDIT.get(org_id, False))


@pytest.fixture
def call(monkeypatch):
    import routes.monitor.chat_audit_routes as mod

    def _run(handler_name, user, org, path, *, args=(), result=None):
        # Patch the globals the RBAC wrapper actually closes over: other tests
        # may re-import utils.auth.rbac_decorators, leaving a stale module object.
        rbac_globals = getattr(mod, handler_name).__globals__
        for name, value in {
            "get_user_id_from_request": lambda: user,
            "get_org_id_from_request": lambda: org,
            "_audit_auth_failure": lambda *a, **k: None,
            "enforce_with_reload": lambda u, o, res, act: (res, act) in GRANTS.get(ROLES.get((u, o)), set()),
        }.items():
            monkeypatch.setitem(rbac_globals, name, value)
        monkeypatch.setattr(mod, "get_org_id_from_request", lambda: org)
        monkeypatch.setattr("utils.auth.viewer_run.may_audit_chats", _fake_may_audit)
        pool = MagicMock()
        monkeypatch.setattr(mod, "db_pool", pool)
        rls = MagicMock()
        monkeypatch.setattr(mod, "set_rls_context", rls)
        listing = MagicMock(return_value=result if result is not None else {"chats": []})
        transcript = MagicMock(return_value=result)
        failed = MagicMock(return_value=result if result is not None else {"tool_calls": []})
        monkeypatch.setattr(mod, "list_chats", listing)
        monkeypatch.setattr(mod, "get_transcript", transcript)
        monkeypatch.setattr(mod, "list_tool_calls", failed)

        app = Flask(__name__)
        with app.test_request_context(path):
            rv = getattr(mod, handler_name)(*args)
            resp, status = rv if isinstance(rv, tuple) else (rv, rv.status_code)
        return resp.get_json(), status, {"rls": rls, "list": listing, "transcript": transcript, "failed": failed}

    return _run


def test_admin_can_list_org_chats(call):
    body, status, m = call("list_org_chats", "admin-1", "org-1", "/api/monitor/chats?source=slack&q=forbidden")
    assert status == 200 and body == {"chats": []}
    assert m["rls"].call_args.kwargs["org_id"] == "org-1"
    _, org_id, filters = m["list"].call_args.args
    assert org_id == "org-1" and filters.source == "slack" and filters.query == "forbidden"


@pytest.mark.parametrize("handler, args, path", [
    ("list_org_chats", (), "/api/monitor/chats"),
    ("get_org_chat", ("s1",), "/api/monitor/chats/s1"),
])
def test_non_admin_is_forbidden(call, handler, args, path):
    body, status, m = call(handler, "viewer-1", "org-1", path, args=args, result={"chat": {}})
    assert status == 403
    m["list"].assert_not_called()
    m["transcript"].assert_not_called()


def test_admin_of_another_org_cannot_read_this_orgs_chat(call):
    # admin-2 is admin in org-2 only; acting in org-1 is denied by RBAC.
    _, status, m = call("get_org_chat", "admin-2", "org-1", "/api/monitor/chats/s1", args=("s1",))
    assert status == 403
    m["transcript"].assert_not_called()


def test_chat_outside_callers_org_is_404(call):
    # The service scopes by org and returns None for another org's chat id.
    body, status, m = call("get_org_chat", "admin-2", "org-2", "/api/monitor/chats/s1", args=("s1",), result=None)
    assert status == 404
    assert m["transcript"].call_args.args[1:] == ("org-2", "s1")


def test_transcript_ok(call):
    body, status, _ = call("get_org_chat", "admin-1", "org-1", "/api/monitor/chats/s1", args=("s1",),
                           result={"chat": {"id": "s1"}, "messages": []})
    assert status == 200 and body["chat"]["id"] == "s1"


def test_bad_session_id_is_400(call):
    body, status, m = call("get_org_chat", "admin-1", "org-1", "/api/monitor/chats/x", args=("../etc",))
    assert status == 400 and body["error"] == "Invalid chat id"
    m["transcript"].assert_not_called()


def test_bad_filter_is_400_with_constant_message(call):
    body, status, _ = call("list_org_chats", "admin-1", "org-1", "/api/monitor/chats?source=<b>x</b>")
    assert status == 400 and "<b>" not in body["error"]


def test_there_is_no_write_route():
    import routes.monitor.chat_audit_routes as mod
    app = Flask(__name__)
    app.register_blueprint(mod.chat_audit_bp)
    methods = {m for rule in app.url_map.iter_rules() if rule.rule.startswith("/api/monitor/chats")
               for m in rule.methods}
    assert methods <= {"GET", "HEAD", "OPTIONS"}



# --- GET /api/monitor/tool-calls (failed tool calls view) ------------------

def test_admin_lists_failed_tool_calls_scoped_to_their_org(call):
    body, status, m = call("list_org_tool_calls", "admin-1", "org-1",
                           "/api/monitor/tool-calls?issue=missing_permission&tool=cloud_exec")
    assert status == 200 and body == {"tool_calls": []}
    assert m["rls"].call_args.kwargs["org_id"] == "org-1"
    _, org_id, raw = m["failed"].call_args.args
    assert org_id == "org-1" and raw["issue"] == "missing_permission" and raw["tool"] == "cloud_exec"


@pytest.mark.parametrize("user, org", [("viewer-1", "org-1"), ("admin-2", "org-1")])
def test_failed_tool_calls_forbidden_for_non_admins_and_other_orgs(call, user, org):
    _, status, m = call("list_org_tool_calls", user, org, "/api/monitor/tool-calls")
    assert status == 403
    m["failed"].assert_not_called()


def test_failed_tool_calls_bad_filter_is_400_with_constant_message(call):
    import routes.monitor.chat_audit_routes as mod
    from services.monitor.chat_audit import FILTER_ERRORS, InvalidFilter

    # First call installs the fixture's patches; then make the service reject the filter.
    _, _, m = call("list_org_tool_calls", "admin-1", "org-1", "/api/monitor/tool-calls")
    m["failed"].side_effect = InvalidFilter("issue")
    body, status, _ = _rerun(mod, "admin-1", "org-1", "/api/monitor/tool-calls?issue=<b>x</b>")
    assert status == 400 and body["error"] == FILTER_ERRORS["issue"] and "<b>" not in body["error"]


def _rerun(mod, user, org, path):
    # Re-invoke the handler with the fixture's patches still in place.
    app = Flask(__name__)
    with app.test_request_context(path):
        rv = mod.list_org_tool_calls()
        resp, status = rv if isinstance(rv, tuple) else (rv, rv.status_code)
    return resp.get_json(), status, None



def test_tool_calls_bad_status_is_400_with_constant_message(call):
    import routes.monitor.chat_audit_routes as mod
    from services.monitor.chat_audit import FILTER_ERRORS, list_tool_calls

    _, _, m = call("list_org_tool_calls", "admin-1", "org-1", "/api/monitor/tool-calls")
    m["failed"].side_effect = lambda cur, org_id, raw: list_tool_calls(cur, org_id, raw)
    body, status, _ = _rerun(mod, "admin-1", "org-1", "/api/monitor/tool-calls?status=<b>x</b>")
    assert status == 400 and body["error"] == FILTER_ERRORS["step_status"]



# --- the org's conversation-audit setting ------------------------------------

@pytest.mark.parametrize("handler, args, path", [
    ("list_org_chats", (), "/api/monitor/chats"),
    ("get_org_chat", ("s1",), "/api/monitor/chats/s1"),
    ("list_org_tool_calls", (), "/api/monitor/tool-calls"),
])
def test_editors_follow_the_org_setting(call, monkeypatch, handler, args, path):
    monkeypatch.setitem(EDITORS_MAY_AUDIT, "org-1", False)
    body, status, _ = call(handler, "editor-1", "org-1", path, args=args, result={"chat": {}})
    assert status == 403 and body["error"] == "Conversation audit isn't enabled for your role."
    monkeypatch.setitem(EDITORS_MAY_AUDIT, "org-1", True)
    _, status, _ = call(handler, "editor-1", "org-1", path, args=args, result={"chat": {}})
    assert status == 200


def test_access_is_checked_before_input_validation(call):
    # A caller without access learns nothing from a bad filter: 403, not 400.
    _, status, _ = call("list_org_chats", "viewer-1", "org-1", "/api/monitor/chats?source=<b>x</b>")
    assert status == 403
    _, status, _ = call("get_org_chat", "viewer-1", "org-1", "/api/monitor/chats/x", args=("../etc",))
    assert status == 403
