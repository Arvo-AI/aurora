"""Tier-2 connector-gated MCP tools.

Each tool is registered at startup so the FastMCP schema is stable, but
every call checks SkillRegistry.check_connection for the user resolved
from the bearer token. If no enabling skill is connected, the call returns
a structured error pointing the user at the Aurora UI to connect the
integration. Combined with per-request visibility filtering (in
mcp_server.py), this implements the "rebuild on every request" gating
model called out in the design.

Every path here has been verified against the Flask url_map — only real
endpoints are reachable. Tools whose backing connectors expose no REST
data-query routes (Coroot, ThousandEyes, distributed tracing across all
connectors, GitHub `rca`) have been dropped — the agent's Python tools
for those are reachable through `chat_with_aurora` instead.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .registry import GatedToolSpec, TIER2_TOOLS, _check_skill_connected
from .response import truncate_payload

logger = logging.getLogger(__name__)

ApiCall = Callable[..., Awaitable[Dict[str, Any]]]


def _not_connected_error(spec: GatedToolSpec) -> Dict[str, Any]:
    return {
        "error": "not_connected",
        "tool": spec.name,
        "message": (
            f"None of the required integrations ({', '.join(spec.enabling_skills)}) "
            f"are connected for this user. Connect at least one in the Aurora UI."
        ),
    }


def _first_connected(skills: List[str], user_id: str) -> Optional[str]:
    for s in skills:
        if _check_skill_connected(s, user_id):
            return s
    return None


_SPEC_BY_NAME: Dict[str, GatedToolSpec] = {s.name: s for s in TIER2_TOOLS}


def register_tier2_tools(
    mcp,
    api_call: ApiCall,
    get_token: Callable[[], str],
    resolve_token: Callable[[str], Any],
) -> None:

    def _user_id() -> str:
        token = get_token()
        uid, _ = resolve_token(token)
        return uid

    # ------------------------------------------------------------------
    # query_logs — Datadog + Splunk only (no logs endpoint exists for
    # newrelic/coroot/dynatrace).
    # ------------------------------------------------------------------
    @mcp.tool()
    async def query_logs(
        query: str,
        source: Optional[str] = None,
        time_range_minutes: int = 60,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """Query logs. Pass `source` to pin a backend (datadog/splunk); omit to
        let Aurora pick the first connected one. Advanced — for investigations
        prefer chat_with_aurora."""
        spec = _SPEC_BY_NAME["query_logs"]
        user_id = _user_id()
        candidates: List[str] = [source] if source else list(spec.enabling_skills)
        chosen = _first_connected(candidates, user_id)
        if chosen is None:
            return _not_connected_error(spec)

        if chosen == "datadog":
            body = {"query": query, "time_range_minutes": time_range_minutes, "limit": limit}
            return truncate_payload(
                await api_call("POST", "/datadog/logs/search", body=body),
                tool_name="query_logs",
            )
        if chosen == "splunk":
            body = {"query": query, "max_count": limit}
            return truncate_payload(
                await api_call("POST", "/splunk/search", body=body),
                tool_name="query_logs",
            )
        # Unreachable today — fires only if TIER2_TOOLS['query_logs'].enabling_skills
        # is extended without a matching branch above. Raise to make the gap loud.
        raise AssertionError(f"query_logs: no dispatch branch for source {chosen!r}")

    # ------------------------------------------------------------------
    # query_metrics — Datadog only.
    # ------------------------------------------------------------------
    @mcp.tool()
    async def query_metrics(
        query: str,
        time_range_minutes: int = 60,
    ) -> Dict[str, Any]:
        """Query metrics. Currently routes to Datadog's metrics query API."""
        spec = _SPEC_BY_NAME["query_metrics"]
        user_id = _user_id()
        if not _check_skill_connected("datadog", user_id):
            return _not_connected_error(spec)
        return truncate_payload(
            await api_call(
                "POST", "/datadog/metrics/query",
                body={"query": query, "time_range_minutes": time_range_minutes},
            ),
            tool_name="query_metrics",
        )

    # ------------------------------------------------------------------
    # query_alerts — multi-source. Each backend has a single "alerts" or
    # "events/ingested" GET endpoint.
    # ------------------------------------------------------------------
    @mcp.tool()
    async def query_alerts(
        source: Optional[str] = None,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """Read alerts from a connected alerting source. Pass `source` to pin
        one of: datadog, newrelic, dynatrace, opsgenie, incidentio, splunk."""
        spec = _SPEC_BY_NAME["query_alerts"]
        user_id = _user_id()
        candidates: List[str] = [source] if source else list(spec.enabling_skills)
        chosen = _first_connected(candidates, user_id)
        if chosen is None:
            return _not_connected_error(spec)

        path_by_source = {
            "datadog": "/datadog/monitors",
            "newrelic": "/newrelic/issues",
            "dynatrace": "/dynatrace/alerts",
            "opsgenie": "/opsgenie/events/ingested",
            "incidentio": "/incidentio/alerts",
            "splunk": "/splunk/alerts",
        }
        path = path_by_source.get(chosen)
        if not path:
            return {"error": "unsupported_source", "source": chosen}

        return truncate_payload(
            await api_call("GET", path, params={"limit": limit}),
            tool_name="query_alerts",
        )

    # ------------------------------------------------------------------
    # query_jira — search + get_issue.
    # ------------------------------------------------------------------
    @mcp.tool()
    async def query_jira(
        action: str,
        jql: Optional[str] = None,
        issue_key: Optional[str] = None,
        max_results: int = 25,
    ) -> Dict[str, Any]:
        """Read Jira. `action` is one of: search, get_issue. Pass `jql` for
        search, `issue_key` for get_issue."""
        spec = _SPEC_BY_NAME["query_jira"]
        user_id = _user_id()
        if not _check_skill_connected("jira", user_id):
            return _not_connected_error(spec)

        if action == "search":
            if not jql:
                return {"error": "jql_required"}
            return truncate_payload(
                await api_call(
                    "POST", "/jira/search",
                    body={"jql": jql, "maxResults": max_results},
                ),
                tool_name="query_jira",
            )
        if action == "get_issue":
            if not issue_key:
                return {"error": "issue_key_required"}
            return truncate_payload(
                await api_call("GET", f"/jira/issue/{issue_key}"),
                tool_name="query_jira",
            )
        return {"error": "invalid_action", "valid_actions": ["search", "get_issue"]}

    # ------------------------------------------------------------------
    # query_notion — only /notion/databases endpoints exist.
    # ------------------------------------------------------------------
    @mcp.tool()
    async def query_notion(
        action: str,
        db_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Read Notion. `action`: list_databases, get_database (requires db_id)."""
        spec = _SPEC_BY_NAME["query_notion"]
        user_id = _user_id()
        if not _check_skill_connected("notion", user_id):
            return _not_connected_error(spec)

        if action == "list_databases":
            return truncate_payload(
                await api_call("GET", "/notion/databases"),
                tool_name="query_notion",
            )
        if action == "get_database":
            if not db_id:
                return {"error": "db_id_required"}
            return truncate_payload(
                await api_call("GET", f"/notion/databases/{db_id}"),
                tool_name="query_notion",
            )
        return {
            "error": "invalid_action",
            "valid_actions": ["list_databases", "get_database"],
        }

    # ------------------------------------------------------------------
    # query_bitbucket — workspace-scoped reads.
    # ------------------------------------------------------------------
    @mcp.tool()
    async def query_bitbucket(
        action: str,
        workspace: Optional[str] = None,
        repo_slug: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Read Bitbucket. `action`: list_workspaces, list_repos, list_branches,
        list_prs. Pass `workspace` (the URL slug) for everything except
        list_workspaces. `list_branches`/`list_prs` also require `repo_slug`."""
        spec = _SPEC_BY_NAME["query_bitbucket"]
        user_id = _user_id()
        if not _check_skill_connected("bitbucket", user_id):
            return _not_connected_error(spec)

        if action == "list_workspaces":
            return truncate_payload(
                await api_call("GET", "/bitbucket/workspaces"),
                tool_name="query_bitbucket",
            )
        if action == "list_repos":
            if not workspace:
                return {"error": "workspace_required"}
            return truncate_payload(
                await api_call("GET", f"/bitbucket/repos/{workspace}"),
                tool_name="query_bitbucket",
            )
        if action == "list_branches":
            if not workspace or not repo_slug:
                return {"error": "workspace_and_repo_slug_required"}
            return truncate_payload(
                await api_call("GET", f"/bitbucket/branches/{workspace}/{repo_slug}"),
                tool_name="query_bitbucket",
            )
        if action == "list_prs":
            if not workspace or not repo_slug:
                return {"error": "workspace_and_repo_slug_required"}
            return truncate_payload(
                await api_call("GET", f"/bitbucket/pull-requests/{workspace}/{repo_slug}"),
                tool_name="query_bitbucket",
            )
        return {
            "error": "invalid_action",
            "valid_actions": ["list_workspaces", "list_repos", "list_branches", "list_prs"],
        }
