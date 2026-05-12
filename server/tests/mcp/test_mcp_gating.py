"""Tests for per-user Tier-2 gating via SkillRegistry.check_connection."""

from __future__ import annotations

import asyncio
from typing import List

import pytest

from aurora_mcp import registry, tools_gated

from .conftest import FakeMCP, make_captured_api_call


def _wire(monkeypatch, connected: List[str]):
    fake = FakeMCP()
    api_call, captured = make_captured_api_call()

    # tools_gated imports _check_skill_connected from registry at module load,
    # so patch BOTH bindings to keep the gate consistent.
    fake_check = lambda skill, user: skill in connected
    monkeypatch.setattr(registry, "_check_skill_connected", fake_check)
    monkeypatch.setattr(tools_gated, "_check_skill_connected", fake_check)

    tools_gated.register_tier2_tools(
        fake, api_call,
        lambda: "tok",
        lambda t: ("user-1", "org-1"),
    )
    return fake.tools, captured


def test_query_logs_returns_not_connected_with_no_connectors(monkeypatch):
    tools, _ = _wire(monkeypatch, connected=[])
    result = asyncio.run(tools["query_logs"](query="error"))
    assert result["error"] == "not_connected"


def test_query_logs_routes_to_first_connected_source(monkeypatch):
    tools, captured = _wire(monkeypatch, connected=["splunk"])
    result = asyncio.run(tools["query_logs"](query="error"))
    assert "error" not in result
    assert captured[-1][1] == "/splunk/search"


def test_query_logs_respects_explicit_source(monkeypatch):
    tools, captured = _wire(monkeypatch, connected=["datadog", "splunk"])
    asyncio.run(tools["query_logs"](query="error", source="splunk"))
    assert captured[-1][1] == "/splunk/search"


def test_query_jira_action_validation(monkeypatch):
    tools, _ = _wire(monkeypatch, connected=["jira"])
    bad = asyncio.run(tools["query_jira"](action="delete"))
    assert bad["error"] == "invalid_action"

    bad_args = asyncio.run(tools["query_jira"](action="search"))
    assert bad_args["error"] == "jql_required"


def test_query_jira_routes_get_issue_with_path_arg(monkeypatch):
    tools, captured = _wire(monkeypatch, connected=["jira"])
    asyncio.run(tools["query_jira"](action="get_issue", issue_key="PROJ-1"))
    assert captured[-1] == ("GET", "/jira/issue/PROJ-1", None, None)


def test_query_alerts_routes_per_source(monkeypatch):
    """query_alerts dispatches to the right endpoint per connected source."""
    expected = {
        "datadog": "/datadog/monitors",
        "newrelic": "/newrelic/issues",
        "dynatrace": "/dynatrace/alerts",
        "opsgenie": "/opsgenie/events/ingested",
        "incidentio": "/incidentio/alerts",
        "splunk": "/splunk/alerts",
    }
    for src, path in expected.items():
        tools, captured = _wire(monkeypatch, connected=[src])
        asyncio.run(tools["query_alerts"]())
        assert captured[-1][1] == path, src


def test_query_notion_validates_action_and_path(monkeypatch):
    tools, captured = _wire(monkeypatch, connected=["notion"])

    asyncio.run(tools["query_notion"](action="list_databases"))
    assert captured[-1] == ("GET", "/notion/databases", None, None)

    asyncio.run(tools["query_notion"](action="get_database", db_id="abc-123"))
    assert captured[-1] == ("GET", "/notion/databases/abc-123", None, None)

    err = asyncio.run(tools["query_notion"](action="get_database"))
    assert err["error"] == "db_id_required"


def test_query_bitbucket_requires_workspace_and_repo(monkeypatch):
    tools, captured = _wire(monkeypatch, connected=["bitbucket"])

    asyncio.run(tools["query_bitbucket"](action="list_workspaces"))
    assert captured[-1] == ("GET", "/bitbucket/workspaces", None, None)

    err = asyncio.run(tools["query_bitbucket"](action="list_branches", workspace="ws"))
    assert err["error"] == "workspace_and_repo_slug_required"

    asyncio.run(tools["query_bitbucket"](
        action="list_prs", workspace="ws", repo_slug="api",
    ))
    assert captured[-1] == ("GET", "/bitbucket/pull-requests/ws/api", None, None)


def test_each_tier2_tool_is_registered(monkeypatch):
    """Every spec in TIER2_TOOLS becomes a callable tool."""
    tools, _ = _wire(monkeypatch, connected=[])
    for spec in registry.TIER2_TOOLS:
        assert spec.name in tools, spec.name
