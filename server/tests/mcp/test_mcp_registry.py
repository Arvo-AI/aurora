"""Unit tests for the MCP registry — allowlist invariants and gating."""

from __future__ import annotations

from aurora_mcp import registry
from aurora_mcp.tools_always_on import register_tier1_tools

from .conftest import FakeMCP, make_captured_api_call


def test_allowlist_excludes_infra_writes():
    """Hard banned name fragments must not appear in the allowlist."""
    names = [e.name.lower() for e in registry.DISPATCH_ALLOWLIST]
    for frag in registry._BANNED_NAME_FRAGMENTS:
        for name in names:
            assert frag not in name, f"banned fragment '{frag}' leaked into '{name}'"


def test_allowlist_assertion_runs_at_import_time():
    """assert_allowlist_safe should be safe on the shipped allowlist."""
    # Just calling it again should not raise.
    registry.assert_allowlist_safe()


def test_tier2_descriptions_are_short():
    """Each Tier-2 description fits in a few sentences (token budget)."""
    for spec in registry.TIER2_TOOLS:
        assert 10 < len(spec.description) < 400, spec.name


def test_find_dispatch_entry_round_trips():
    entry = registry.DISPATCH_ALLOWLIST[0]
    assert registry.find_dispatch_entry(entry.name) is entry
    assert registry.find_dispatch_entry("definitely-not-a-tool") is None


def test_search_dispatch_entries_query_filter():
    out = registry.search_dispatch_entries(query="jira", user_id=None, limit=50)
    assert all("jira" in (e.name + e.description).lower() for e in out)
    assert any(e.name.startswith("jira_") for e in out)


def test_search_dispatch_entries_multi_word_query():
    """A natural-language query matches if ANY token hits name/description.

    The old whole-string matcher joined the query with underscores and found
    nothing for multi-word queries; the tokenized matcher must find the
    deployment entries and rank them ahead of looser matches."""
    out = registry.search_dispatch_entries(
        query="list recent deployments", user_id=None, limit=10,
    )
    names = [e.name for e in out]
    assert names, "multi-word query should return results"
    # The three deployment entries match the most tokens, so they rank first.
    assert "jenkins_list_deployments" in names
    assert names[0].endswith("_list_deployments")


def test_search_dispatch_entries_empty_query_returns_up_to_limit():
    out = registry.search_dispatch_entries(query="", user_id=None, limit=5)
    assert len(out) == 5


def test_promoted_graph_tools_removed_from_allowlist():
    """The 3 graph/infra reads are now first-class Tier-1 tools, not dispatch
    entries — they must not appear twice."""
    names = {e.name for e in registry.DISPATCH_ALLOWLIST}
    for promoted in (
        "get_infrastructure_context", "graph_list_services", "graph_service_impact",
    ):
        assert promoted not in names, f"{promoted} should be promoted, not in allowlist"
    # The rest of the graph family stays in dispatch.
    assert "graph_get_full" in names
    assert "graph_get_service" in names


def test_phantom_github_list_repos_removed():
    """github_list_repos mapped to a non-existent /github/repos route."""
    names = {e.name for e in registry.DISPATCH_ALLOWLIST}
    assert "github_list_repos" not in names
    paths = {e.path for e in registry.DISPATCH_ALLOWLIST}
    assert "/github/repos" not in paths


def test_new_connector_entries_present_and_gated():
    """CI/CD + Sentry + Grafana read entries exist and are skill-gated."""
    by_name = {e.name: e for e in registry.DISPATCH_ALLOWLIST}
    expected = {
        "jenkins_list_deployments": "jenkins",
        "cloudbees_list_deployments": "cloudbees",
        "spinnaker_list_deployments": "spinnaker",
        "spinnaker_list_applications": "spinnaker",
        "spinnaker_list_pipelines": "spinnaker",
        "spinnaker_list_pipeline_configs": "spinnaker",
        "spinnaker_app_health": "spinnaker",
        "sentry_list_projects": "sentry",
        "sentry_list_issues": "sentry",
        "sentry_list_events": "sentry",
        "grafana_list_alerts": "grafana",
    }
    for name, skill in expected.items():
        assert name in by_name, f"{name} missing from allowlist"
        entry = by_name[name]
        assert entry.enabling_skills == (skill,), name
        assert entry.method == "GET", name


def test_new_connector_entries_gated_in_search(monkeypatch):
    """Gated entries are only callable_now once their skill is connected."""
    monkeypatch.setattr(
        registry, "_check_skill_connected", lambda s, u: s == "sentry",
    )
    visible = registry.search_dispatch_entries(
        query="sentry", user_id="u1", limit=50,
    )
    assert any(e.name == "sentry_list_issues" for e in visible)
    # A non-connected connector is filtered out when user_id is supplied.
    not_visible = registry.search_dispatch_entries(
        query="grafana", user_id="u1", limit=50,
    )
    assert not any(e.name == "grafana_list_alerts" for e in not_visible)


def test_promoted_tools_registered_as_tier1():
    """The 6 curated reads are first-class Tier-1 tools."""
    fake = FakeMCP()
    api_call, _ = make_captured_api_call()
    register_tier1_tools(fake, api_call)
    for name in (
        "get_infrastructure_context", "list_services", "service_impact",
        "incident_findings", "incident_finding_detail", "incident_list_alerts",
    ):
        assert name in fake.tools, name


def test_service_impact_url_encodes_name():
    """A service name with a slash/space must not break the path."""
    import asyncio

    fake = FakeMCP()
    api_call, captured = make_captured_api_call()
    register_tier1_tools(fake, api_call)
    asyncio.run(fake.tools["service_impact"](name="payments/v2 svc"))
    _, path, _, _ = captured[-1]
    assert path == "/api/graph/services/payments%2Fv2%20svc/impact"


def test_list_services_passes_filter_params():
    import asyncio

    fake = FakeMCP()
    api_call, captured = make_captured_api_call()
    register_tier1_tools(fake, api_call)
    asyncio.run(fake.tools["list_services"](resource_type="db", provider="aws"))
    method, path, params, _ = captured[-1]
    assert (method, path) == ("GET", "/api/graph/services")
    assert params == {"resource_type": "db", "provider": "aws"}


def test_search_dispatch_entries_category_filter():
    out = registry.search_dispatch_entries(category="ticketing", user_id=None, limit=50)
    assert out
    assert all(e.category == "ticketing" for e in out)


def test_gated_tool_visibility_uses_check_connection(monkeypatch):
    """Tier-2 specs become visible only when at least one enabler is connected."""
    seen = {"jira": False, "datadog": True}

    def fake_check(skill_id: str, user_id: str) -> bool:
        return seen.get(skill_id, False)

    monkeypatch.setattr(registry, "_check_skill_connected", fake_check)

    by_name = {s.name: s for s in registry.TIER2_TOOLS}
    assert registry.gated_tool_visible(by_name["query_logs"], "u1") is True      # datadog ✓
    assert registry.gated_tool_visible(by_name["query_jira"], "u1") is False     # jira ✗

    seen["jira"] = True
    assert registry.gated_tool_visible(by_name["query_jira"], "u1") is True


def test_always_on_dispatch_entries_visible_without_skills(monkeypatch):
    """Entries with no enabling_skills are visible regardless of connections."""
    monkeypatch.setattr(registry, "_check_skill_connected", lambda s, u: False)
    always_on = [e for e in registry.DISPATCH_ALLOWLIST if not e.enabling_skills]
    assert always_on, "registry must include at least one always-on dispatch entry"
    for entry in always_on:
        assert registry.dispatch_entry_visible(entry, "u1") is True
