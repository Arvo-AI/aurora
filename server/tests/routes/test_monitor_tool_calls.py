"""GET /api/monitor/tool-calls (DEV-1729)."""

from unittest.mock import MagicMock

import pytest
from flask import Flask


@pytest.fixture
def call(monkeypatch):
    import routes.monitor.tool_call_routes as mod

    def _run(query_string="", org_id="org-1", search_result=None, raises=None):
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        pool = MagicMock()
        pool.get_admin_connection.return_value.__enter__.return_value = conn
        monkeypatch.setattr(mod, "db_pool", pool)
        monkeypatch.setattr(mod, "get_org_id_from_request", lambda: org_id)
        rls = MagicMock()
        monkeypatch.setattr(mod, "set_rls_context", rls)
        search = MagicMock(return_value=search_result or {"items": []}, side_effect=raises)
        monkeypatch.setattr(mod, "search_tool_calls", search)

        handler = mod.list_tool_calls
        while hasattr(handler, "__wrapped__"):
            handler = handler.__wrapped__
        app = Flask(__name__)
        with app.test_request_context(f"/api/monitor/tool-calls{query_string}"):
            resp, status = handler("user-1")
        return resp.get_json(), status, rls, search

    return _run


def test_route_is_rbac_protected():
    import routes.monitor.tool_call_routes as mod
    assert hasattr(mod.list_tool_calls, "__wrapped__")


def test_scopes_rls_to_the_request_org_and_passes_filters(call):
    body, status, rls, search = call("?status=failed&period=7d&q=AccessDenied")
    assert status == 200 and body == {"items": []}
    assert rls.call_args.kwargs["org_id"] == "org-1"
    _, org_id, filters = search.call_args.args
    assert org_id == "org-1"
    assert (filters.status, filters.period, filters.query) == ("failed", "7d", "AccessDenied")


def test_bad_filter_is_400(call):
    body, status, _, search = call("?status=nope")
    assert status == 400 and "status" in body["error"]
    search.assert_not_called()


def test_missing_org_is_403(call):
    _, status, _, search = call(org_id=None)
    assert status == 403
    search.assert_not_called()


def test_db_failure_is_500_without_details(call):
    body, status, _, _ = call(raises=RuntimeError("password=leak"))
    assert status == 500
    assert "leak" not in body["error"]
