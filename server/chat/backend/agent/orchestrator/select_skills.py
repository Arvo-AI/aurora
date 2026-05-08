"""Tool selection utilities for sub-agents.

Sub-agents are read-only. Tools are filtered by:
1. Non-mutating (mutates != True in metadata)
2. Capability tags intersecting role's allowlist
3. Approximate 4000-token budget, priority-ordered by rca_priority
"""

import logging
from typing import Any

from chat.backend.agent.orchestrator.tool_cache import wrap_tool_with_cache

logger = logging.getLogger(__name__)

_SKILL_TOKEN_BUDGET = 4_000  # approximate tokens of tool-spec description budget
_SUBAGENT_SKILL_BUDGET = 4_000  # tokens — budget for skill bodies appended to sub-agent briefs

# First-class cloud providers (handled outside the SKILL.md system via direct
# user_tokens / user_connections). cloud_exec is also valid for these even
# when no skill claims it.
_CLOUD_PROVIDERS = frozenset({"aws", "gcp", "azure", "ovh", "scaleway"})

# Capability tag dispatch table for the most-commonly-used RCA tools.
# Tools NOT listed here default to: mutates=False, cacheable=False, capability_tags=[]
# (i.e. they stay available to the lead but excluded from sub-agent tool subsets).
_TOOL_METADATA: dict = {
    # Generic CLI execution. mutates=False because mutation safety is enforced
    # per-command by the guardrails layer (signature matcher + LLM judge), not
    # by this static flag. Sub-agents need cloud_exec to actually query state.
    "cloud_exec": {"capability_tags": ["runtime_state", "metrics", "logs", "observability"], "mutates": False, "cacheable": False},
    "on_prem_kubectl": {"capability_tags": ["runtime_state", "observability"], "mutates": False, "cacheable": False},
    "terminal_exec": {"capability_tags": ["runtime_state"], "mutates": False, "cacheable": False},
    "tailscale_ssh": {"capability_tags": ["runtime_state"], "mutates": False, "cacheable": False},
    # Observability platforms — read-only query tools
    "query_datadog": {"capability_tags": ["metrics", "observability", "error_tracking", "logs"], "mutates": False, "cacheable": True},
    "query_newrelic": {"capability_tags": ["metrics", "observability", "error_tracking"], "mutates": False, "cacheable": True},
    "query_dynatrace": {"capability_tags": ["metrics", "observability", "error_tracking"], "mutates": False, "cacheable": True},
    "query_opsgenie": {"capability_tags": ["on_call", "ticket_history"], "mutates": False, "cacheable": True},
    "search_splunk": {"capability_tags": ["logs", "observability"], "mutates": False, "cacheable": True},
    "list_splunk_indexes": {"capability_tags": ["logs"], "mutates": False, "cacheable": True},
    "list_splunk_sourcetypes": {"capability_tags": ["logs"], "mutates": False, "cacheable": True},
    "spinnaker_rca": {"capability_tags": ["ci_cd"], "mutates": False, "cacheable": True},
    # Source control — read-only
    "github_rca": {"capability_tags": ["source_control_read", "ci_cd"], "mutates": False, "cacheable": True},
    "get_connected_repos": {"capability_tags": ["source_control_read"], "mutates": False, "cacheable": False},
    # Write tools — excluded from sub-agents
    "github_commit": {"capability_tags": ["source_control_write"], "mutates": True, "cacheable": False},
    "github_fix": {"capability_tags": ["source_control_write"], "mutates": True, "cacheable": False},
    "github_apply_fix": {"capability_tags": ["source_control_write"], "mutates": True, "cacheable": False},
    "iac_tool": {"capability_tags": ["iac"], "mutates": True, "cacheable": False},
    # Runbooks + knowledge base
    "confluence_runbook_parse": {"capability_tags": ["runbooks", "knowledge_base"], "mutates": False, "cacheable": True},
    "knowledge_base_search": {"capability_tags": ["knowledge_base", "runbooks"], "mutates": False, "cacheable": True},
    # Ticket / incident history
    "list_incidentio_incidents": {"capability_tags": ["ticket_history", "on_call"], "mutates": False, "cacheable": True},
    "get_incidentio_incident": {"capability_tags": ["ticket_history", "on_call"], "mutates": False, "cacheable": True},
    "get_incidentio_timeline": {"capability_tags": ["ticket_history", "on_call"], "mutates": False, "cacheable": True},
    # General research
    "web_search": {"capability_tags": ["knowledge_base"], "mutates": False, "cacheable": True},
    # Bitbucket — source control + CI (mirror of github tagging)
    "bitbucket_repos": {"capability_tags": ["source_control_read"], "mutates": False, "cacheable": True},
    "bitbucket_branches": {"capability_tags": ["source_control_read"], "mutates": False, "cacheable": True},
    "bitbucket_pull_requests": {"capability_tags": ["source_control_read", "ci_cd"], "mutates": False, "cacheable": True},
    "bitbucket_pipelines": {"capability_tags": ["ci_cd"], "mutates": False, "cacheable": True},
    "bitbucket_issues": {"capability_tags": ["ticket_history"], "mutates": False, "cacheable": True},
    # CI/CD providers (alongside spinnaker_rca above)
    "cloudbees_rca": {"capability_tags": ["ci_cd"], "mutates": False, "cacheable": True},
    "jenkins_rca": {"capability_tags": ["ci_cd"], "mutates": False, "cacheable": True},
    # Cloudflare — read-only query tools; cloudflare_action mutates
    "query_cloudflare": {"capability_tags": ["metrics", "observability", "logs"], "mutates": False, "cacheable": True},
    "cloudflare_list_zones": {"capability_tags": ["observability"], "mutates": False, "cacheable": True},
    "cloudflare_action": {"capability_tags": [], "mutates": True, "cacheable": False},
    # Confluence runbook-search (confluence_runbook_parse already above)
    "confluence_search_similar": {"capability_tags": ["runbooks", "knowledge_base"], "mutates": False, "cacheable": True},
    "confluence_search_runbooks": {"capability_tags": ["runbooks", "knowledge_base"], "mutates": False, "cacheable": True},
    "confluence_fetch_page": {"capability_tags": ["runbooks", "knowledge_base"], "mutates": False, "cacheable": True},
    # Coroot — observability platform
    "coroot_get_incidents": {"capability_tags": ["ticket_history", "observability"], "mutates": False, "cacheable": True},
    "coroot_get_incident_detail": {"capability_tags": ["ticket_history", "observability"], "mutates": False, "cacheable": True},
    "coroot_get_applications": {"capability_tags": ["runtime_state", "observability"], "mutates": False, "cacheable": True},
    "coroot_get_app_detail": {"capability_tags": ["runtime_state", "observability"], "mutates": False, "cacheable": True},
    "coroot_get_app_logs": {"capability_tags": ["logs", "observability"], "mutates": False, "cacheable": True},
    "coroot_get_traces": {"capability_tags": ["metrics", "observability"], "mutates": False, "cacheable": True},
    "coroot_get_service_map": {"capability_tags": ["runtime_state", "observability"], "mutates": False, "cacheable": True},
    "coroot_query_metrics": {"capability_tags": ["metrics", "observability"], "mutates": False, "cacheable": True},
    "coroot_get_deployments": {"capability_tags": ["ci_cd"], "mutates": False, "cacheable": True},
    "coroot_get_nodes": {"capability_tags": ["runtime_state"], "mutates": False, "cacheable": True},
    "coroot_get_overview_logs": {"capability_tags": ["logs", "observability"], "mutates": False, "cacheable": True},
    "coroot_get_node_detail": {"capability_tags": ["runtime_state"], "mutates": False, "cacheable": True},
    "coroot_get_risks": {"capability_tags": ["observability"], "mutates": False, "cacheable": True},
    # Jira — ticket history (read) vs mutate (write)
    "jira_search_issues": {"capability_tags": ["ticket_history"], "mutates": False, "cacheable": True},
    "jira_get_issue": {"capability_tags": ["ticket_history"], "mutates": False, "cacheable": True},
    "jira_add_comment": {"capability_tags": [], "mutates": True, "cacheable": False},
    "jira_create_issue": {"capability_tags": [], "mutates": True, "cacheable": False},
    "jira_update_issue": {"capability_tags": [], "mutates": True, "cacheable": False},
    "jira_link_issues": {"capability_tags": [], "mutates": True, "cacheable": False},
    # Notion — read-only investigation tools (write tools default to mutates=True via the catch-all below)
    "notion_search": {"capability_tags": ["runbooks", "knowledge_base"], "mutates": False, "cacheable": True},
    "notion_fetch": {"capability_tags": ["runbooks", "knowledge_base"], "mutates": False, "cacheable": True},
    "notion_query_database": {"capability_tags": ["knowledge_base"], "mutates": False, "cacheable": True},
    "notion_query_data_source": {"capability_tags": ["knowledge_base"], "mutates": False, "cacheable": True},
    "notion_get_block_children": {"capability_tags": ["knowledge_base"], "mutates": False, "cacheable": True},
    # SharePoint
    "sharepoint_search": {"capability_tags": ["runbooks", "knowledge_base"], "mutates": False, "cacheable": True},
    "sharepoint_fetch_page": {"capability_tags": ["runbooks", "knowledge_base"], "mutates": False, "cacheable": True},
    "sharepoint_fetch_document": {"capability_tags": ["runbooks", "knowledge_base"], "mutates": False, "cacheable": True},
    "sharepoint_create_page": {"capability_tags": [], "mutates": True, "cacheable": False},
    # ThousandEyes — network observability
    "thousandeyes_list_tests": {"capability_tags": ["metrics", "observability"], "mutates": False, "cacheable": True},
    "thousandeyes_get_test_detail": {"capability_tags": ["metrics", "observability"], "mutates": False, "cacheable": True},
    "thousandeyes_get_test_results": {"capability_tags": ["metrics", "observability"], "mutates": False, "cacheable": True},
    "thousandeyes_get_alerts": {"capability_tags": ["ticket_history", "observability"], "mutates": False, "cacheable": True},
    "thousandeyes_get_alert_rules": {"capability_tags": ["observability"], "mutates": False, "cacheable": True},
    "thousandeyes_get_agents": {"capability_tags": ["runtime_state"], "mutates": False, "cacheable": True},
    "thousandeyes_get_endpoint_agents": {"capability_tags": ["runtime_state"], "mutates": False, "cacheable": True},
    "thousandeyes_get_internet_insights": {"capability_tags": ["observability", "metrics"], "mutates": False, "cacheable": True},
    "thousandeyes_get_dashboards": {"capability_tags": ["metrics", "observability"], "mutates": False, "cacheable": True},
    "thousandeyes_get_dashboard_widget": {"capability_tags": ["metrics", "observability"], "mutates": False, "cacheable": True},
    "thousandeyes_get_bgp_monitors": {"capability_tags": ["observability"], "mutates": False, "cacheable": True},
}


def _get_tool_meta(tool) -> dict:
    name = getattr(tool, "name", "")
    base = {"capability_tags": [], "mutates": False, "cacheable": False}
    override = _TOOL_METADATA.get(name, {})
    tool_md = getattr(tool, "metadata", None) or {}
    return {**base, **tool_md, **override}


def get_available_capability_tags(user_id: str) -> set:
    """Return capability tags reachable for *this* user.

    A tag is considered available only if it is contributed by a tool that
    would survive ``select_tools_for_role``'s connection gate for ``user_id``:
    either a non-skill-gated built-in (e.g. ``cloud_exec``, ``web_search``)
    or a skill-owned tool whose owning skill the user has connected.
    """
    try:
        from chat.backend.agent.tools.cloud_tools import get_cloud_tools
        skill_owned, connected = _resolve_connected_tool_filter(user_id)
        # Mirror select_tools_for_role's cloud_exec special-case: it's also
        # valid when a first-class cloud provider (gcp/aws/azure) is connected,
        # not just when an ovh/scaleway/tailscale skill claims it.
        has_cloud_provider = False
        try:
            from chat.background.rca_prompt_builder import get_user_providers
            connected_providers = get_user_providers(user_id) or []
            has_cloud_provider = any(p.lower() in _CLOUD_PROVIDERS for p in connected_providers)
        except Exception:
            logger.exception("select_skills: failed to resolve connected providers for tags")
        tools = get_cloud_tools()
        tags: set = set()
        for t in tools:
            tool_name = getattr(t, "name", "")
            if tool_name in skill_owned and tool_name not in connected:
                if not (tool_name == "cloud_exec" and has_cloud_provider):
                    continue
            meta = _get_tool_meta(t)
            # Sub-agents are read-only; mutating tools never reach select_tools_for_role,
            # so they shouldn't contribute capability tags either.
            if meta.get("mutates"):
                continue
            tags.update(meta.get("capability_tags", []))
        return tags
    except Exception:
        logger.exception("select_skills: failed to resolve available capability tags")
        return set()


def _resolve_connected_tool_filter(user_id: str) -> tuple[set, set]:
    """Return ``(skill_owned_tools, connected_tools)`` for connection gating.

    - ``skill_owned_tools``: every tool name listed by ANY registered skill.
      Tools NOT in this set are considered always-available built-ins
      (e.g. ``cloud_exec``, ``knowledge_base_search``, ``web_search``).
    - ``connected_tools``: tools owned by at least one skill the user has
      currently connected. A skill-owned tool only passes the filter if it
      appears here.
    """
    skill_owned: set = set()
    connected: set = set()
    try:
        from chat.backend.agent.skills.registry import SkillRegistry
        registry = SkillRegistry.get_instance()
        connected_ids = set(registry.get_connected_skill_ids(user_id) or [])
        for skill_id, meta in registry._skills.items():
            for tool_name in (meta.tools or []):
                skill_owned.add(tool_name)
                if skill_id in connected_ids:
                    connected.add(tool_name)
    except Exception:
        logger.exception("select_skills: failed to resolve connected-tool filter for user")
    return skill_owned, connected


def select_tools_for_role(user_id: str, role: Any, all_tools: list) -> list:
    role_tags = set(role.tools)
    skill_owned, connected = _resolve_connected_tool_filter(user_id)

    # `cloud_exec` is a built-in (not skill-owned) but only useful when the
    # user has at least one cloud provider connected. Otherwise the brief
    # already tells the LLM not to call it — strip it to save tokens.
    has_cloud_provider = False
    try:
        from chat.background.rca_prompt_builder import get_user_providers
        connected_providers = get_user_providers(user_id) or []
        has_cloud_provider = any(p.lower() in _CLOUD_PROVIDERS for p in connected_providers)
    except Exception:
        logger.exception("select_skills: failed to resolve connected providers for user")

    candidates: list = []
    dropped_unconnected: list = []
    dropped_cloud_exec = False
    for tool in all_tools:
        meta = _get_tool_meta(tool)
        if meta.get("mutates"):
            continue
        if not role_tags.intersection(set(meta.get("capability_tags", []))):
            continue
        tool_name = getattr(tool, "name", "")
        if tool_name in skill_owned and tool_name not in connected:
            # cloud_exec is special — some skills (ovh/scaleway/tailscale) claim
            # it via their SKILL.md, but it ALSO works for first-class cloud
            # providers (gcp/aws/azure) which aren't skills. Let it through
            # whenever any cloud provider is connected.
            if tool_name == "cloud_exec" and has_cloud_provider:
                pass
            else:
                dropped_unconnected.append(tool_name)
                continue
        if tool_name == "cloud_exec" and not has_cloud_provider:
            dropped_cloud_exec = True
            continue
        candidates.append((tool, meta))
    if dropped_unconnected:
        logger.info(
            "select_skills: role=%s dropped %d unconnected tools: %s",
            role.name, len(dropped_unconnected), sorted(set(dropped_unconnected)),
        )
    if dropped_cloud_exec:
        logger.info(
            "select_skills: role=%s dropped cloud_exec (no cloud providers connected)",
            role.name,
        )

    _CHARS_PER_TOKEN = 4
    budget_chars = _SKILL_TOKEN_BUDGET * _CHARS_PER_TOKEN
    selected: list = []
    used_chars = 0
    for tool, meta in candidates:
        desc = getattr(tool, "description", "") or ""
        tool_chars = len(desc) + 200
        if used_chars + tool_chars > budget_chars:
            logger.debug(
                "select_skills: token budget reached at %d tools for role %s",
                len(selected), role.name,
            )
            break
        selected.append(wrap_tool_with_cache(tool, tool_metadata=meta))
        used_chars += tool_chars

    logger.info(
        "select_skills: role=%s tags=%s -> %d/%d tools",
        role.name, sorted(role_tags), len(selected), len(all_tools),
    )
    return selected


def load_skills_for_role(user_id: str, role: Any) -> str:
    """Return concatenated skill markdown for skills connected to this user
    whose tools' capability_tags intersect role.tools, priority-ordered by
    rca_priority and capped at _SUBAGENT_SKILL_BUDGET tokens. Never raises."""
    try:
        from chat.backend.agent.skills.registry import SkillRegistry
        from chat.backend.agent.skills.loader import estimate_tokens

        registry = SkillRegistry.get_instance()
        role_tags = set(role.tools)
        matches: list = []  # (meta, ctx_data)
        for skill_id, meta in registry._skills.items():
            skill_tags: set = set()
            for tool_name in meta.tools:
                skill_tags.update(_TOOL_METADATA.get(tool_name, {}).get("capability_tags", []))
            if not skill_tags or not (role_tags & skill_tags):
                continue
            is_connected, ctx_data = registry.check_connection(skill_id, user_id)
            if not is_connected:
                continue
            matches.append((meta, ctx_data))

        matches.sort(key=lambda pair: pair[0].rca_priority)

        parts: list = []
        tokens_used = 0
        for meta, ctx_data in matches:
            if tokens_used >= _SUBAGENT_SKILL_BUDGET:
                break
            result = registry.load_skill(meta.id, user_id, _prevalidated_context=ctx_data)
            if result.is_connected and result.content:
                est = result.token_estimate or estimate_tokens(result.content)
                if tokens_used + est > _SUBAGENT_SKILL_BUDGET:
                    break
                parts.append(result.content)
                tokens_used += est

        if parts:
            logger.info(
                "select_skills: role=%s loaded %d skills (~%d tokens)",
                role.name, len(parts), tokens_used,
            )
        return "\n\n".join(parts)
    except Exception:
        logger.exception("select_skills: load_skills_for_role failed for role=%s", getattr(role, "name", "?"))
        return ""
