"""Sentry web API query tool for the RCA agent.

Supports issue search, issue detail fetch (with full stacktrace), project
listing, and Discover-style event queries via the Sentry web API.
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from connectors.sentry_connector.client import SentryClient, SentryAPIError
from routes.sentry.config import (
    MAX_OUTPUT_SIZE,
    MAX_QUERY_LENGTH,
    MAX_RESULTS_CAP,
)
from routes.sentry.sentry_routes import (
    _build_client_from_creds,
    _get_stored_sentry_credentials,
)

logger = logging.getLogger(__name__)

_VALID_RESOURCE_TYPES = (
    "issues",
    "issue_detail",
    "issue_event",
    "projects",
    "events",
)
_RESOURCE_HELP = ", ".join(f"'{r}'" for r in _VALID_RESOURCE_TYPES)

_STATS_PERIOD_RE = re.compile(r"^\d+[mhdw]$")
_ISSUE_ID_RE = re.compile(r"^[0-9]+$")


class QuerySentryArgs(BaseModel):
    resource_type: str = Field(
        description=(
            "Type of Sentry query. One of: "
            "'issues' — search issues by Sentry query string. "
            "'issue_detail' — fetch metadata for one issue (query=issue_id). "
            "'issue_event' — fetch latest event with full stacktrace (query=issue_id). "
            "'projects' — list projects in the org. "
            "'events' — Discover-style event table search."
        )
    )
    query: str = Field(
        default="",
        description=(
            "Query string. Meaning depends on resource_type:\n"
            "  issues: Sentry search expression, e.g. \"is:unresolved level:error environment:production\". "
            "Defaults to 'is:unresolved' if empty.\n"
            "  issue_detail / issue_event: The numeric Sentry issue id (required).\n"
            "  projects: Ignored.\n"
            "  events: Discover query expression, e.g. \"level:error project:my-service\"."
        ),
    )
    stats_period: str = Field(
        default="24h",
        description=(
            "Sentry stats_period. Format: Nm, Nh, Nd, Nw (e.g. '15m', '24h', '7d'). "
            "Used for 'issues' and 'events' resource types; ignored for issue_detail/issue_event/projects."
        ),
    )
    project: str = Field(
        default="",
        description="Optional project slug filter (comma-separated for multiple). Applies to 'issues' and 'events'.",
    )
    environment: str = Field(
        default="",
        description="Optional environment filter (e.g. 'production'). Applies to 'issues' and 'events'.",
    )
    limit: int = Field(
        default=25,
        description="Maximum results to return (default: 25, max: 100).",
    )


def _truncate_results(results: list, max_size: int = MAX_OUTPUT_SIZE) -> Tuple[list, bool]:
    """Truncate a list of results to stay within the byte budget."""
    truncated: List[Any] = []
    total_size = 0
    for item in results:
        item_str = json.dumps(item, default=str)
        item_len = len(item_str)
        if item_len > 50_000:
            if isinstance(item, dict):
                shrunk = {
                    k: (str(v)[:2000] + "...[truncated]" if isinstance(v, str) and len(v) > 2000 else v)
                    for k, v in item.items()
                }
                item = shrunk
                item_str = json.dumps(item, default=str)
                item_len = len(item_str)
        if total_size + item_len > max_size:
            return truncated, True
        truncated.append(item)
        total_size += item_len
    return truncated, False


def is_sentry_connected(user_id: str) -> bool:
    """Check if a user has valid Sentry credentials stored."""
    creds = _get_stored_sentry_credentials(user_id)
    if not creds:
        return False
    try:
        return _build_client_from_creds(creds) is not None
    except ValueError as exc:
        logger.warning("[SENTRY-TOOL] Invalid stored credentials for user=%s: %s", user_id, exc)
        return False


def _get_client(user_id: str) -> Tuple[Optional[SentryClient], Optional[str]]:
    """Return (client, error_json_or_None)."""
    creds = _get_stored_sentry_credentials(user_id)
    if not creds:
        return None, json.dumps({"error": "Sentry not connected. Please connect Sentry first."})
    try:
        client = _build_client_from_creds(creds)
    except ValueError as exc:
        logger.warning("[SENTRY-TOOL] Invalid stored credentials for user=%s: %s", user_id, exc)
        return None, json.dumps({"error": "Stored Sentry credentials are invalid. Please reconnect."})
    if not client:
        return None, json.dumps({"error": "Sentry credentials are incomplete. Please reconnect."})
    return client, None


def _validate_query(query: str, allow_empty: bool = True) -> Optional[str]:
    """Validate a Sentry query string for size; reject anything obviously malformed."""
    if not query:
        return None if allow_empty else "Query is required for this resource_type."
    if len(query) > MAX_QUERY_LENGTH:
        return f"Query exceeds maximum length ({MAX_QUERY_LENGTH} chars)."
    return None


def _validate_stats_period(stats_period: str) -> Optional[str]:
    """Reject stats_period strings that don't match the Sentry Nm/Nh/Nd/Nw pattern."""
    if not stats_period:
        return None
    if not _STATS_PERIOD_RE.match(stats_period.strip()):
        return (
            "stats_period must be a number followed by one of m/h/d/w "
            "(e.g. '15m', '24h', '7d')."
        )
    return None


def _parse_project_filter(project: str) -> Optional[List[str]]:
    """Parse a comma-separated project slug filter into a list."""
    if not project:
        return None
    return [p.strip() for p in project.split(",") if p.strip()] or None


def _handle_issues(
    client: SentryClient,
    query: str,
    stats_period: str,
    project: Optional[List[str]],
    environment: str,
    limit: int,
) -> Dict[str, Any]:
    err = _validate_query(query) or _validate_stats_period(stats_period)
    if err:
        return {"error": err}

    effective_query = query.strip() or "is:unresolved"
    data = client.list_issues(
        query=effective_query,
        stats_period=stats_period or "24h",
        project=project,
        environment=environment or None,
        limit=limit,
    )
    return {
        "resource_type": "issues",
        "query": effective_query,
        "stats_period": stats_period or "24h",
        "count": data.get("count", 0),
        "results": data.get("issues", []),
        "next_cursor": data.get("next_cursor"),
    }


def _handle_issue_detail(client: SentryClient, query: str) -> Dict[str, Any]:
    if not query or not _ISSUE_ID_RE.match(query.strip()):
        return {"error": "issue_detail requires a numeric Sentry issue id as the query parameter."}
    issue = client.get_issue(query.strip())
    return {
        "resource_type": "issue_detail",
        "issue_id": query.strip(),
        "results": [issue],
        "count": 1,
    }


def _handle_issue_event(client: SentryClient, query: str) -> Dict[str, Any]:
    if not query or not _ISSUE_ID_RE.match(query.strip()):
        return {"error": "issue_event requires a numeric Sentry issue id as the query parameter."}
    event = client.get_issue_latest_event(query.strip())
    return {
        "resource_type": "issue_event",
        "issue_id": query.strip(),
        "results": [event],
        "count": 1,
    }


def _handle_projects(client: SentryClient, limit: int) -> Dict[str, Any]:
    projects = client.list_projects(limit=limit)
    return {
        "resource_type": "projects",
        "count": len(projects),
        "results": [
            {
                "id": p.get("id"),
                "slug": p.get("slug"),
                "name": p.get("name"),
                "platform": p.get("platform"),
                "isMember": p.get("isMember"),
                "features": p.get("features"),
            }
            for p in projects
        ],
    }


def _handle_events(
    client: SentryClient,
    query: str,
    stats_period: str,
    project: Optional[List[str]],
    environment: str,
    limit: int,
) -> Dict[str, Any]:
    err = _validate_query(query) or _validate_stats_period(stats_period)
    if err:
        return {"error": err}

    data = client.search_events(
        query=query or "",
        stats_period=stats_period or "24h",
        per_page=limit,
        project=project,
        environment=environment or None,
    )
    return {
        "resource_type": "events",
        "query": query,
        "stats_period": stats_period or "24h",
        "count": data.get("count", 0),
        "results": data.get("events", []),
        "meta": data.get("meta", {}),
    }


def query_sentry(
    resource_type: str,
    query: str = "",
    stats_period: str = "24h",
    project: str = "",
    environment: str = "",
    limit: int = 25,
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    """Query Sentry for issues, full event details, projects, or Discover-style event tables.

    Returns a JSON string with the query results or an error message.
    """
    if not user_id:
        return json.dumps({"error": "User context not available"})

    client, err = _get_client(user_id)
    if err or client is None:
        return err or json.dumps({"error": "Sentry client unavailable"})

    resource_type = (resource_type or "").lower().strip()
    if resource_type not in _VALID_RESOURCE_TYPES:
        return json.dumps({
            "error": f"Invalid resource_type '{resource_type}'. Must be one of: {_RESOURCE_HELP}",
        })

    limit = min(max(limit, 1), MAX_RESULTS_CAP)
    project_filter = _parse_project_filter(project)

    logger.info(
        "[SENTRY-TOOL] user=%s resource=%s query=%s",
        user_id, resource_type, (query[:100] if query else ""),
    )

    try:
        if resource_type == "issues":
            result = _handle_issues(client, query, stats_period, project_filter, environment, limit)
        elif resource_type == "issue_detail":
            result = _handle_issue_detail(client, query)
        elif resource_type == "issue_event":
            result = _handle_issue_event(client, query)
        elif resource_type == "projects":
            result = _handle_projects(client, limit)
        elif resource_type == "events":
            result = _handle_events(client, query, stats_period, project_filter, environment, limit)
        else:
            return json.dumps({"error": f"Unhandled resource_type '{resource_type}'"})

        if "error" in result:
            return json.dumps(result)

        result["success"] = True
        result["org_slug"] = client.org_slug
        result["region"] = client.region

        results_list = result.get("results", [])
        truncated_results, was_truncated = _truncate_results(results_list)
        if was_truncated:
            result["results"] = truncated_results
            result["truncated"] = True
            result["note"] = (
                f"Results truncated from {len(results_list)} to {len(truncated_results)} due to size limit. "
                "Use a more specific query (add level:, environment:, release: filters) to narrow results."
            )
            result["count"] = len(truncated_results)

        return json.dumps(result, default=str)

    except SentryAPIError as exc:
        status = exc.status_code
        if status == 429:
            return json.dumps({"error": "Sentry API rate limit reached. Wait a moment and retry."})
        if status in (401, 403):
            return json.dumps({"error": "Sentry authentication failed. Auth token may be invalid or revoked."})
        if status == 404:
            return json.dumps({"error": "Sentry resource not found (issue, project, or org slug)."})
        return json.dumps({"error": f"Sentry API error: {str(exc)[:200]}"})
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    except Exception:
        logger.exception("[SENTRY-TOOL] Query failed for user=%s resource=%s", user_id, resource_type)
        return json.dumps({"error": "Internal error while querying Sentry"})
