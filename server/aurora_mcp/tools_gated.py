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
for those are reachable through the Aurora UI agent instead.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

from .registry import GatedToolSpec, TIER2_TOOLS, _check_skill_connected
from .response import truncate_payload

logger = logging.getLogger(__name__)

ApiCall = Callable[..., Awaitable[Dict[str, Any]]]


def _rfc3339_window(time_range_minutes: int) -> Tuple[str, str]:
    """Return (from_rfc3339, to_rfc3339) — Datadog logs API shape."""
    now = datetime.now(timezone.utc)
    return (now - timedelta(minutes=time_range_minutes)).isoformat(), now.isoformat()


def _epoch_ms_window(time_range_minutes: int) -> Tuple[int, int]:
    """Return (from_ms, to_ms) — Datadog metrics API shape."""
    to_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    from_ms = to_ms - time_range_minutes * 60 * 1000
    return from_ms, to_ms


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

# Defensive bounds on LLM-supplied numeric args. Tier-2 calls go through to
# Datadog/Splunk/Jira so a hostile or sloppy caller could otherwise pass
# negative values (Datadog returns 400, Splunk computes -X earliest = future)
# or huge ones (forcing upstream paginate-and-return-everything semantics).
_MIN_TIME_RANGE_MINUTES = 1
_MAX_TIME_RANGE_MINUTES = 1440  # 24h — matches the longest window any caller asks for
_MIN_LIMIT = 1
_MAX_LIMIT = 500


def _clamp_minutes(value: Any) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = 60
    return max(_MIN_TIME_RANGE_MINUTES, min(v, _MAX_TIME_RANGE_MINUTES))


def _clamp_limit(value: Any) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = 50
    return max(_MIN_LIMIT, min(v, _MAX_LIMIT))


def _resolve_source(
    tool_name: str, user_id: str, source: Optional[str]
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Return (chosen_source, error_payload). Exactly one is non-None."""
    spec = _SPEC_BY_NAME[tool_name]
    candidates: List[str] = [source] if source else list(spec.enabling_skills)
    chosen = _first_connected(candidates, user_id)
    if chosen is None:
        return None, _not_connected_error(spec)
    return chosen, None


_ALERTS_PATH_BY_SOURCE: Dict[str, str] = {
    "datadog": "/datadog/monitors",
    "newrelic": "/newrelic/issues",
    "dynatrace": "/dynatrace/alerts",
    "opsgenie": "/opsgenie/events/ingested",
    "incidentio": "/incidentio/alerts",
    "splunk": "/splunk/alerts",
}

# Fail fast at import if someone extends query_alerts' enabling_skills without
# adding the matching dispatch path here — otherwise the runtime fallback
# would silently surface an unsupported_source error.
assert set(_ALERTS_PATH_BY_SOURCE) >= set(
    _SPEC_BY_NAME["query_alerts"].enabling_skills
), (
    "query_alerts enabling_skills not fully covered by _ALERTS_PATH_BY_SOURCE: "
    f"missing {set(_SPEC_BY_NAME['query_alerts'].enabling_skills) - set(_ALERTS_PATH_BY_SOURCE)}"
)


async def _do_query_logs(
    api_call: ApiCall, user_id: str, query: str,
    source: Optional[str], time_range_minutes: int, limit: int,
) -> Dict[str, Any]:
    chosen, err = _resolve_source("query_logs", user_id, source)
    if err is not None:
        return err
    clamped_minutes = _clamp_minutes(time_range_minutes)
    clamped_limit = _clamp_limit(limit)
    if chosen == "datadog":
        from_iso, to_iso = _rfc3339_window(clamped_minutes)
        body = {"query": query, "from": from_iso, "to": to_iso, "limit": clamped_limit}
        return truncate_payload(
            await api_call("POST", "/datadog/logs/search", body=body),
            tool_name="query_logs",
        )
    if chosen == "splunk":
        body = {
            "query": query,
            "earliestTime": f"-{clamped_minutes}m",
            "latestTime": "now",
            "maxCount": clamped_limit,
        }
        return truncate_payload(
            await api_call("POST", "/splunk/search", body=body),
            tool_name="query_logs",
        )
    # Unreachable today — fires only if TIER2_TOOLS["query_logs"].enabling_skills
    # is extended without a matching branch above. Raise to make the gap loud.
    raise AssertionError(f"query_logs: no dispatch branch for source {chosen!r}")


async def _do_query_metrics(
    api_call: ApiCall, user_id: str, query: str, time_range_minutes: int,
) -> Dict[str, Any]:
    spec = _SPEC_BY_NAME["query_metrics"]
    if not _check_skill_connected("datadog", user_id):
        return _not_connected_error(spec)
    from_ms, to_ms = _epoch_ms_window(_clamp_minutes(time_range_minutes))
    return truncate_payload(
        await api_call(
            "POST", "/datadog/metrics/query",
            body={"query": query, "fromMs": from_ms, "toMs": to_ms},
        ),
        tool_name="query_metrics",
    )


async def _do_query_alerts(
    api_call: ApiCall, user_id: str, source: Optional[str], limit: int,
) -> Dict[str, Any]:
    chosen, err = _resolve_source("query_alerts", user_id, source)
    if err is not None:
        return err
    path = _ALERTS_PATH_BY_SOURCE.get(chosen or "")
    if not path:
        return {"error": "unsupported_source", "source": chosen}
    return truncate_payload(
        await api_call("GET", path, params={"limit": _clamp_limit(limit)}),
        tool_name="query_alerts",
    )


async def _do_query_jira(
    api_call: ApiCall, user_id: str, action: str,
    jql: Optional[str], issue_key: Optional[str], max_results: int,
) -> Dict[str, Any]:
    spec = _SPEC_BY_NAME["query_jira"]
    if not _check_skill_connected("jira", user_id):
        return _not_connected_error(spec)
    if action == "search":
        if not jql:
            return {"error": "jql_required"}
        return truncate_payload(
            await api_call(
                "POST", "/jira/search",
                body={"jql": jql, "maxResults": _clamp_limit(max_results)},
            ),
            tool_name="query_jira",
        )
    if action == "get_issue":
        if not issue_key:
            return {"error": "issue_key_required"}
        return truncate_payload(
            await api_call("GET", f"/jira/issue/{quote(issue_key, safe='')}"),
            tool_name="query_jira",
        )
    return {"error": "invalid_action", "valid_actions": ["search", "get_issue"]}


async def _do_query_notion(
    api_call: ApiCall, user_id: str, action: str, db_id: Optional[str],
) -> Dict[str, Any]:
    spec = _SPEC_BY_NAME["query_notion"]
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
            await api_call("GET", f"/notion/databases/{quote(db_id, safe='')}"),
            tool_name="query_notion",
        )
    return {
        "error": "invalid_action",
        "valid_actions": ["list_databases", "get_database"],
    }


async def _bitbucket_action(
    api_call: ApiCall, action: str,
    workspace: Optional[str], repo_slug: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Dispatch a single Bitbucket action. Returns None if action is unknown."""
    if action == "list_workspaces":
        return truncate_payload(
            await api_call("GET", "/bitbucket/workspaces"),
            tool_name="query_bitbucket",
        )
    if action == "list_repos":
        if not workspace:
            return {"error": "workspace_required"}
        return truncate_payload(
            await api_call("GET", f"/bitbucket/repos/{quote(workspace, safe='')}"),
            tool_name="query_bitbucket",
        )
    if action in ("list_branches", "list_prs"):
        if not workspace or not repo_slug:
            return {"error": "workspace_and_repo_slug_required"}
        sub = "branches" if action == "list_branches" else "pull-requests"
        return truncate_payload(
            await api_call(
                "GET",
                f"/bitbucket/{sub}/{quote(workspace, safe='')}/{quote(repo_slug, safe='')}",
            ),
            tool_name="query_bitbucket",
        )
    return None


async def _do_query_bitbucket(
    api_call: ApiCall, user_id: str, action: str,
    workspace: Optional[str], repo_slug: Optional[str],
) -> Dict[str, Any]:
    spec = _SPEC_BY_NAME["query_bitbucket"]
    if not _check_skill_connected("bitbucket", user_id):
        return _not_connected_error(spec)
    result = await _bitbucket_action(api_call, action, workspace, repo_slug)
    if result is not None:
        return result
    return {
        "error": "invalid_action",
        "valid_actions": ["list_workspaces", "list_repos", "list_branches", "list_prs"],
    }


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

    @mcp.tool()
    async def query_logs(
        query: str,
        source: Optional[str] = None,
        time_range_minutes: int = 60,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """Query logs. Pass `source` to pin a backend (datadog/splunk); omit to
        let Aurora pick the first connected one."""
        return await _do_query_logs(
            api_call, _user_id(), query, source, time_range_minutes, limit,
        )

    @mcp.tool()
    async def query_metrics(
        query: str,
        time_range_minutes: int = 60,
    ) -> Dict[str, Any]:
        """Query metrics. Currently routes to Datadog's metrics query API.

        `query` must be a full Datadog query expression including the scope.
        Example: `system.cpu.user{*}` or `avg:trace.http.request.duration{env:prod}`.
        Bare metric names (`system.cpu.user`) will fail to parse."""
        return await _do_query_metrics(api_call, _user_id(), query, time_range_minutes)

    @mcp.tool()
    async def query_alerts(
        source: Optional[str] = None,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """Read alerts from a connected alerting source. Pass `source` to pin
        one of: datadog, newrelic, dynatrace, opsgenie, incidentio, splunk."""
        return await _do_query_alerts(api_call, _user_id(), source, limit)

    @mcp.tool()
    async def query_jira(
        action: str,
        jql: Optional[str] = None,
        issue_key: Optional[str] = None,
        max_results: int = 25,
    ) -> Dict[str, Any]:
        """Read Jira. `action` is one of: search, get_issue. Pass `jql` for
        search, `issue_key` for get_issue."""
        return await _do_query_jira(
            api_call, _user_id(), action, jql, issue_key, max_results,
        )

    @mcp.tool()
    async def query_notion(
        action: str,
        db_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Read Notion. `action`: list_databases, get_database (requires db_id)."""
        return await _do_query_notion(api_call, _user_id(), action, db_id)

    @mcp.tool()
    async def query_bitbucket(
        action: str,
        workspace: Optional[str] = None,
        repo_slug: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Read Bitbucket. `action`: list_workspaces, list_repos, list_branches,
        list_prs. Pass `workspace` (the URL slug) for everything except
        list_workspaces. `list_branches`/`list_prs` also require `repo_slug`."""
        return await _do_query_bitbucket(
            api_call, _user_id(), action, workspace, repo_slug,
        )
