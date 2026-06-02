"""Tests for the Tier-3 dispatch (search_tools + call_tool) wiring."""

from __future__ import annotations

import asyncio
from typing import List

import pytest

from aurora_mcp import dispatch, registry

from .conftest import FakeMCP, make_captured_api_call


@pytest.fixture
def api_call_and_captured():
    return make_captured_api_call()


@pytest.fixture
def api_call(api_call_and_captured):
    return api_call_and_captured[0]


@pytest.fixture
def captured_calls(api_call_and_captured):
    return api_call_and_captured[1]


def _wire(monkeypatch, api_call, connected: List[str]):
    """Register dispatch tools with a stubbed connectivity check."""
    fake = FakeMCP()
    monkeypatch.setattr(
        registry, "_check_skill_connected",
        lambda skill, user: skill in connected,
    )
    dispatch.register_dispatch_tools(
        fake, api_call,
        lambda: "test-token",
        lambda token: ("test-user", "test-org"),
    )
    return fake.tools


def test_search_tools_returns_visible_and_not_visible(monkeypatch, api_call):
    tools = _wire(monkeypatch, api_call, connected=["jira"])
    result = asyncio.run(tools["search_tools"](query="jira"))
    names = [t["name"] for t in result["tools"]]
    assert any(n.startswith("jira_") for n in names)
    jira_entries = [t for t in result["tools"] if t["name"].startswith("jira_")]
    assert all(t["callable_now"] for t in jira_entries)


def test_search_tools_marks_not_connected_entries(monkeypatch, api_call):
    tools = _wire(monkeypatch, api_call, connected=[])  # nothing connected
    result = asyncio.run(tools["search_tools"](query="jira"))
    jira_entries = [t for t in result["tools"] if t["name"].startswith("jira_")]
    assert jira_entries, "discovery should still surface jira entries"
    assert all(t["callable_now"] is False for t in jira_entries)


def test_call_tool_rejects_unallowlisted(monkeypatch, api_call):
    tools = _wire(monkeypatch, api_call, connected=["jira"])
    result = asyncio.run(tools["call_tool"]("delete_all_the_things", {}))
    assert result["error"] == "tool_not_allowlisted"


def test_call_tool_rejects_when_not_connected(monkeypatch, api_call):
    tools = _wire(monkeypatch, api_call, connected=[])
    result = asyncio.run(tools["call_tool"]("jira_search_issues", {"jql": "x"}))
    assert result["error"] == "not_connected"


def test_call_tool_substitutes_path_args(monkeypatch, api_call, captured_calls):
    tools = _wire(monkeypatch, api_call, connected=["jira"])
    asyncio.run(tools["call_tool"]("jira_get_issue", {"issue_key": "PROJ-42"}))
    assert captured_calls
    _, path, _, _ = captured_calls[-1]
    assert path == "/jira/issue/PROJ-42"


def test_call_tool_splits_body_vs_params(monkeypatch, api_call, captured_calls):
    tools = _wire(monkeypatch, api_call, connected=["jira"])
    asyncio.run(tools["call_tool"]("jira_search_issues", {
        "jql": "status = Open",
        "maxResults": 50,
        "trailing_unknown": "in-query",
    }))
    method, path, params, body = captured_calls[-1]
    assert method == "POST"
    assert path == "/jira/search"
    assert body == {"jql": "status = Open", "maxResults": 50}
    assert params == {"trailing_unknown": "in-query"}


def test_call_tool_handles_multi_arg_path(monkeypatch, api_call, captured_calls):
    """Tier-3 entries with multiple path_args (e.g. bitbucket workspace+repo)."""
    tools = _wire(monkeypatch, api_call, connected=["bitbucket"])
    asyncio.run(tools["call_tool"]("bitbucket_list_branches", {
        "workspace": "myco", "repo_slug": "api",
    }))
    _, path, _, _ = captured_calls[-1]
    assert path == "/bitbucket/branches/myco/api"


def test_call_tool_missing_path_arg_returns_error(monkeypatch, api_call):
    tools = _wire(monkeypatch, api_call, connected=["jira"])
    result = asyncio.run(tools["call_tool"]("jira_get_issue", {}))
    assert result["error"] == "missing_path_arg"
    assert result["arg"] == "issue_key"


def test_no_terraform_or_kubectl_entries():
    """Defense-in-depth — even if allowlist gets edited, banned ops stay out."""
    for entry in registry.DISPATCH_ALLOWLIST:
        for frag in ("terraform_apply", "kubectl_delete", "terminal_exec"):
            assert frag not in entry.name
            assert frag not in entry.path
