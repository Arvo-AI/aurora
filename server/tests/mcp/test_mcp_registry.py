"""Unit tests for the MCP registry — allowlist invariants and gating."""

from __future__ import annotations

from aurora_mcp import registry


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
