"""The agent's read-only search_tool_calls tool."""

import json
from contextlib import contextmanager
from unittest.mock import MagicMock

import chat.backend.agent.tools.introspection_tools as tools
import services.monitor.tool_call_search as search_mod


def _patch(monkeypatch, result=None):
    cur = MagicMock()

    @contextmanager
    def fake_cursor(user_id):
        yield cur, "org-1"

    monkeypatch.setattr(tools, "_cursor", fake_cursor)
    captured = {}

    def fake_search(c, org_id, filters):
        captured.update(cur=c, org_id=org_id, filters=filters)
        return result or {
            "items": [{"tool_name": "cloud_exec", "error": "x" * 5000, "reason": "AccessDenied"}],
            "total": 1, "summary": {"failed": 1}, "tools": [], "filters": {},
        }

    monkeypatch.setattr(search_mod, "search_tool_calls", fake_search)
    return captured


def test_defaults_to_failed_last_week(monkeypatch):
    captured = _patch(monkeypatch)
    out = json.loads(tools.search_tool_calls(user_id="user-1"))
    f = captured["filters"]
    assert (f.status, f.period, f.per_page) == ("failed", "7d", 20)
    assert captured["org_id"] == "org-1"
    assert out["total_matching"] == 1
    assert len(out["tool_calls"][0]["error"]) <= tools.OUTPUT_EXCERPT_MAX_CHARS


def test_limit_is_capped(monkeypatch):
    captured = _patch(monkeypatch)
    tools.search_tool_calls(limit=500, user_id="user-1")
    assert captured["filters"].per_page == 50


def test_bad_filter_returns_clean_error(monkeypatch):
    _patch(monkeypatch)
    out = json.loads(tools.search_tool_calls(period="forever", user_id="user-1"))
    assert "period" in out["error"]


def test_requires_user():
    assert "error" in json.loads(tools.search_tool_calls())
