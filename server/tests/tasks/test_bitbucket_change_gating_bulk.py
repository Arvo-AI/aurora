"""Bulk Incident Prevention enable/disable — arity and apply contract.

The Flask route queues 4 arguments (user_id, org_id, names, enabled). A
worker still running the 3-arg task raises TypeError; this file pins that
the apply function accepts both 3-arg (enable default) and 4-arg calls.
"""
from __future__ import annotations

import contextlib
import inspect

from routes.bitbucket import bitbucket_selection as sel


class _Cur:
    def __init__(self, rows):
        self.rows = rows
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, rows):
        self.cur = _Cur(rows)

    def cursor(self):
        return self.cur

    def commit(self):
        pass


def _patch_db(monkeypatch, rows):
    @contextlib.contextmanager
    def _conn():
        yield _Conn(rows)

    monkeypatch.setattr(sel.db_pool, "get_admin_connection", _conn)
    monkeypatch.setattr(sel, "set_rls_context", lambda *a, **k: "org-1")


def test_apply_accepts_enabled_as_fourth_arg():
    sig = inspect.signature(sel._apply_change_gating_bulk)
    params = list(sig.parameters)
    assert params == ["user_id", "org_id", "names", "enabled"]
    assert sig.parameters["enabled"].default is True


def test_disable_updates_and_cleans_hooks(monkeypatch):
    _patch_db(monkeypatch, [])
    monkeypatch.setattr(sel, "cleanup_org_hooks", lambda *a: ["ws/r"])
    out = sel._apply_change_gating_bulk("u", "org-1", ["ws/r"], False)
    assert out["org_id"] == "org-1"
    assert out["change_gating_enabled"] is False
    assert out["webhook_cleanup_failed"] is True
    assert out["results"] == [{"repo_full_name": "ws/r"}]


def test_enable_creates_and_reports_hooks(monkeypatch):
    _patch_db(monkeypatch, [("ws/r", "main", False)])
    monkeypatch.setattr(sel, "get_or_create_webhook_secret", lambda org: "secret")
    monkeypatch.setattr(sel, "_webhook_base_url", lambda: "https://api.example.com")
    monkeypatch.setattr(sel, "_try_auto_create_hook", lambda *a: True)
    out = sel._apply_change_gating_bulk("u", "org-1", ["ws/r"], True)
    assert out["org_id"] == "org-1"
    assert out["change_gating_enabled"] is True
    assert out["webhook_url"] == "https://api.example.com/bitbucket/webhook/org-1"
    assert "webhook_secret" not in out
    created = [r for r in out["results"] if r.get("webhook_auto_created") is True]
    assert created == [{"repo_full_name": "ws/r", "webhook_auto_created": True}]


def test_enable_retries_hook_when_already_on_but_unverified(monkeypatch):
    _patch_db(monkeypatch, [("ws/r", "main", False)])
    monkeypatch.setattr(sel, "get_or_create_webhook_secret", lambda org: "secret")
    monkeypatch.setattr(sel, "_webhook_base_url", lambda: "https://api.example.com")
    called = []
    monkeypatch.setattr(sel, "_try_auto_create_hook", lambda *a: called.append(a[2]) or True)
    out = sel._apply_change_gating_bulk("u", "org-1", ["ws/r"], True)
    assert called == ["ws/r"]
    assert any(r.get("webhook_auto_created") is True for r in out["results"])


def test_enable_skips_hook_when_already_verified(monkeypatch):
    _patch_db(monkeypatch, [("ws/r", "main", True)])
    monkeypatch.setattr(sel, "get_or_create_webhook_secret", lambda org: "secret")
    monkeypatch.setattr(sel, "_webhook_base_url", lambda: "https://api.example.com")
    monkeypatch.setattr(
        sel, "_try_auto_create_hook", lambda *a: (_ for _ in ()).throw(AssertionError("should not create"))
    )
    out = sel._apply_change_gating_bulk("u", "org-1", ["ws/r"])
    assert out["change_gating_enabled"] is True
    assert {"repo_full_name": "ws/r"} in out["results"]
