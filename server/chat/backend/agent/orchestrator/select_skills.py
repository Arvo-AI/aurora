"""Tool selection utilities for sub-agents.

Sub-agents are read-only. Tools are filtered by:
1. Non-mutating (mutates != True in metadata)
2. Capability tags intersecting role's allowlist
3. Approximate 4000-token budget, priority-ordered by rca_priority
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

_SKILL_TOKEN_BUDGET = 4_000  # approximate tokens of tool-spec description budget
_SUBAGENT_SKILL_BUDGET = 4_000  # tokens — budget for skill bodies appended to sub-agent briefs

# Capability tag dispatch table for the most-commonly-used RCA tools.
# Tools NOT listed here default to: mutates=False, cacheable=False, capability_tags=[]
# (i.e. they stay available to the lead but excluded from sub-agent tool subsets).
_TOOL_METADATA: dict = {
    # Generic CLI execution. mutates=False because mutation safety is enforced
    # per-command by the guardrails layer (signature matcher + LLM judge), not
    # by this static flag. Sub-agents need cloud_exec to actually query state.
    "cloud_exec": {"capability_tags": ["runtime_state", "metrics", "logs", "observability"], "mutates": False, "cacheable": False},
    "kubectl_onprem": {"capability_tags": ["runtime_state", "observability"], "mutates": False, "cacheable": False},
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
}


def _get_tool_meta(tool) -> dict:
    name = getattr(tool, "name", "")
    base = {"capability_tags": [], "mutates": False, "cacheable": False}
    override = _TOOL_METADATA.get(name, {})
    tool_md = getattr(tool, "metadata", None) or {}
    return {**base, **tool_md, **override}


def _patch_tool_metadata(tools: list) -> None:
    for tool in tools:
        name = getattr(tool, "name", "")
        if name in _TOOL_METADATA and hasattr(tool, "metadata"):
            try:
                if tool.metadata is None:
                    tool.metadata = {}
                tool.metadata.update(_TOOL_METADATA[name])
            except (AttributeError, TypeError):
                continue


def get_available_capability_tags(user_id: str) -> set:
    try:
        from chat.backend.agent.tools.cloud_tools import get_cloud_tools
        tools = get_cloud_tools()
        tags: set = set()
        for t in tools:
            meta = _get_tool_meta(t)
            tags.update(meta.get("capability_tags", []))
        return tags
    except Exception:
        logger.exception("select_skills: failed to resolve available capability tags")
        return set()


def select_tools_for_role(user_id: str, role: Any, all_tools: list) -> list:
    _patch_tool_metadata(all_tools)
    role_tags = set(role.tools)
    candidates = []
    for tool in all_tools:
        meta = _get_tool_meta(tool)
        if meta.get("mutates"):
            continue
        if not role_tags.intersection(set(meta.get("capability_tags", []))):
            continue
        candidates.append(tool)

    _CHARS_PER_TOKEN = 4
    budget_chars = _SKILL_TOKEN_BUDGET * _CHARS_PER_TOKEN
    selected: list = []
    used_chars = 0
    for tool in candidates:
        desc = getattr(tool, "description", "") or ""
        tool_chars = len(desc) + 200
        if used_chars + tool_chars > budget_chars:
            logger.debug(
                "select_skills: token budget reached at %d tools for role %s",
                len(selected), role.name,
            )
            break
        selected.append(tool)
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
                parts.append(result.content)
                tokens_used += result.token_estimate or estimate_tokens(result.content)

        if parts:
            logger.info(
                "select_skills: role=%s loaded %d skills (~%d tokens)",
                role.name, len(parts), tokens_used,
            )
        return "\n\n".join(parts)
    except Exception:
        logger.exception("select_skills: load_skills_for_role failed for role=%s", getattr(role, "name", "?"))
        return ""
