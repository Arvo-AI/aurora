"""The agent's read-only search_tool_calls tool."""

import json
from contextlib import contextmanager
from unittest.mock import MagicMock

import chat.backend.agent.tools.introspection_tools as tools
import services.monitor.chat_audit as audit


def _patch(monkeypatch):
    cur = MagicMock()
    captured = {}

    @contextmanager
    def fake_cursor(user_id):
        yield cur, "org-1"

    def fake_search(c, org_id, raw, limit, owner_user_id=None):
        captured.update(cur=c, org_id=org_id, raw=raw, limit=limit, owner_user_id=owner_user_id)
        return {"tool_calls": [{"tool_name": "cloud_exec", "reason": "AccessDenied"}], "by_issue": [], "period": "7d"}

    monkeypatch.setattr(tools, "_cursor", fake_cursor)
    monkeypatch.setattr(audit, "search_failed_tool_calls", fake_search)
    return cur, captured


def test_defaults_to_last_week_scoped_to_the_callers_org(monkeypatch):
    cur, captured = _patch(monkeypatch)
    out = json.loads(tools.search_tool_calls(user_id="user-1"))
    assert captured["cur"] is cur and captured["org_id"] == "org-1"
    assert captured["raw"]["period"] == "7d" and captured["limit"] == 20
    assert out["tool_calls"][0]["reason"] == "AccessDenied"


def test_passes_filters_and_caps_limit(monkeypatch):
    _, captured = _patch(monkeypatch)
    tools.search_tool_calls(issue="blocked_command", tool="cloud_exec", query="s3", limit=500, user_id="u")
    assert captured["raw"] == {"period": "7d", "issue": "blocked_command", "tool": "cloud_exec",
                               "q": "s3", "source": None}
    assert captured["limit"] == 50


def test_bad_filter_returns_constant_error(monkeypatch):
    @contextmanager
    def fake_cursor(user_id):
        yield MagicMock(), "org-1"

    monkeypatch.setattr(tools, "_cursor", fake_cursor)
    out = json.loads(tools.search_tool_calls(period="forever", user_id="user-1"))
    assert out["error"] == audit.FILTER_ERRORS["period"]


def test_requires_user():
    assert "error" in json.loads(tools.search_tool_calls())


def _role(monitor, monkeypatch, *, admin=None, raises=False):
    import utils.auth.enforcer as enforcer

    def fake(user_id, org_id, resource, action):
        monitor.append((user_id, org_id, resource, action))
        if raises:
            raise RuntimeError("casbin down")
        return admin

    monkeypatch.setattr(enforcer, "enforce_with_reload", fake)


def test_admin_keeps_the_org_scope(monkeypatch):
    _, captured = _patch(monkeypatch)
    checks = []
    _role(checks, monkeypatch, admin=True)
    tools.search_tool_calls(user_id="admin-1")
    assert checks == [("admin-1", "org-1", "admin", "access")]
    assert captured["owner_user_id"] is None


def test_non_admin_is_limited_to_own_chats(monkeypatch):
    _, captured = _patch(monkeypatch)
    _role([], monkeypatch, admin=False)
    tools.search_tool_calls(user_id="viewer-1")
    assert captured["owner_user_id"] == "viewer-1"


def test_failed_role_check_fails_closed(monkeypatch):
    _, captured = _patch(monkeypatch)
    _role([], monkeypatch, raises=True)
    tools.search_tool_calls(user_id="viewer-1")
    assert captured["owner_user_id"] == "viewer-1"


def test_non_admin_cannot_widen_scope_through_arguments(monkeypatch):
    _, captured = _patch(monkeypatch)
    _role([], monkeypatch, admin=False)
    tools.search_tool_calls(user_id="viewer-1", owner_user_id=None, scope="org", source="incident")
    assert captured["owner_user_id"] == "viewer-1"
    # The schema the model sees has no way to name a scope or another user.
    assert not {"owner_user_id", "scope", "user", "user_id"} & set(tools.SearchToolCallsArgs.model_fields)
