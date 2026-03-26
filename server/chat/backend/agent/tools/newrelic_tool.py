"""New Relic NerdGraph query tool for the RCA agent.

Supports NRQL queries (logs, metrics, transactions, errors, infrastructure),
entity search, and alert issue retrieval — all via the NerdGraph GraphQL API.
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from connectors.newrelic_connector.client import NewRelicClient, NewRelicAPIError
from routes.newrelic.config import MAX_NRQL_LENGTH, MAX_OUTPUT_SIZE, MAX_RESULTS_CAP
from routes.newrelic.newrelic_routes import (
    _get_stored_newrelic_credentials,
    _build_client_from_creds,
)

logger = logging.getLogger(__name__)

_VALID_RESOURCE_TYPES = (
    "nrql",
    "issues",
    "entities",
)

_RESOURCE_HELP = ", ".join(f"'{r}'" for r in _VALID_RESOURCE_TYPES)


class QueryNewRelicArgs(BaseModel):
    resource_type: str = Field(
        description=(
            "Type of query to run. One of: "
            "'nrql' — execute any NRQL query (logs, metrics, transactions, errors, spans, infrastructure). "
            "'issues' — list active alert issues from New Relic Alerts. "
            "'entities' — search for monitored entities (APM apps, hosts, services, etc.)."
        )
    )
    query: str = Field(
        default="",
        description=(
            "Query string. Meaning depends on resource_type:\n"
            "  nrql: A full NRQL statement, e.g. \"SELECT count(*) FROM Transaction WHERE appName = 'my-app' SINCE 1 hour ago\".\n"
            "  issues: Optional. Filter by state: 'ACTIVATED', 'CREATED', 'CLOSED'. Leave empty for all active issues.\n"
            "  entities: Search term or entity name, e.g. 'production-api'. Optional entity_type filter appended after '|', "
            "e.g. 'production-api|APPLICATION'."
        ),
    )
    time_range: str = Field(
        default="1 hour",
        description=(
            "Time range for NRQL queries when the query doesn't already contain a SINCE clause. "
            "Examples: '1 hour', '30 minutes', '24 hours', '7 days'. Ignored for issues/entities."
        ),
    )
    limit: int = Field(
        default=100,
        description="Maximum results to return (default: 100).",
    )


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

_RELATIVE_RE = re.compile(r"^(\d+)\s*(m(?:in(?:ute)?s?)?|h(?:ours?)?|d(?:ays?)?|w(?:eeks?)?)$", re.IGNORECASE)

_UNIT_MAP = {
    "m": "minutes", "min": "minutes", "mins": "minutes", "minute": "minutes", "minutes": "minutes",
    "h": "hours", "hour": "hours", "hours": "hours",
    "d": "days", "day": "days", "days": "days",
    "w": "weeks", "week": "weeks", "weeks": "weeks",
}


def _parse_time_range(time_range: str) -> str:
    """Convert a human time range like '1 hour' into NRQL SINCE clause value.

    Only accepts structured relative patterns (e.g. '1 hour', '30 minutes').
    Rejects freeform strings to prevent injection of extra NRQL tokens.
    """
    stripped = time_range.strip().lower()
    m = _RELATIVE_RE.match(stripped)
    if m:
        amount = int(m.group(1))
        unit = _UNIT_MAP.get(m.group(2).rstrip("s"), m.group(2))
        return f"{amount} {unit} ago"
    return "1 hour ago"


def _inject_since_clause(nrql: str, time_range: str) -> str:
    """Append a SINCE clause to NRQL only if one isn't already present."""
    upper = nrql.upper()
    if "SINCE" in upper or "UNTIL" in upper:
        return nrql
    since_val = _parse_time_range(time_range)
    return f"{nrql} SINCE {since_val}"


_LIMIT_RE = re.compile(r"\bLIMIT\s+(\d+|MAX)\b", re.IGNORECASE)


def _inject_limit_clause(nrql: str, limit: int) -> str:
    """Ensure the query has a LIMIT clause capped at the given value."""
    cap = min(limit, MAX_RESULTS_CAP)
    m = _LIMIT_RE.search(nrql)
    if m:
        existing = m.group(1)
        if existing.upper() == "MAX" or int(existing) > cap:
            return _LIMIT_RE.sub(f"LIMIT {cap}", nrql, count=1)
        return nrql
    return f"{nrql} LIMIT {cap}"


# ---------------------------------------------------------------------------
# NRQL safety
# ---------------------------------------------------------------------------

_NRQL_DISALLOWED = re.compile(r"\b(DROP|DELETE|INSERT|UPDATE|CREATE|ALTER)\b", re.IGNORECASE)
_QUOTED_STRINGS = re.compile(r"'[^']*'")


def _validate_nrql(nrql: str) -> Optional[str]:
    """Return an error message if the NRQL is unsafe or too long, else None."""
    if not nrql or not nrql.strip():
        return "NRQL query is required for resource_type='nrql'."
    if len(nrql) > MAX_NRQL_LENGTH:
        return f"NRQL query exceeds maximum length ({MAX_NRQL_LENGTH} chars)."
    stripped = _QUOTED_STRINGS.sub("''", nrql)
    if _NRQL_DISALLOWED.search(stripped):
        return "NRQL query contains disallowed keywords (only SELECT/FROM/WHERE/FACET queries are supported)."
    return None


# ---------------------------------------------------------------------------
# Result truncation
# ---------------------------------------------------------------------------

def _truncate_results(results: list, max_size: int = MAX_OUTPUT_SIZE) -> tuple:
    """Truncate a list of results to stay within the byte budget."""
    truncated: List[Any] = []
    total_size = 0
    for item in results:
        item_str = json.dumps(item, default=str)
        item_len = len(item_str)
        if item_len > 10_000:
            if isinstance(item, dict):
                item = {k: (str(v)[:800] + "...[truncated]" if isinstance(v, str) and len(v) > 800 else v)
                        for k, v in item.items()}
                item_str = json.dumps(item, default=str)
                item_len = len(item_str)
        if total_size + item_len > max_size:
            return truncated, True
        truncated.append(item)
        total_size += item_len
    return truncated, False


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

def is_newrelic_connected(user_id: str) -> bool:
    """Check if a user has valid New Relic credentials stored."""
    creds = _get_stored_newrelic_credentials(user_id)
    if not creds:
        return False
    try:
        return _build_client_from_creds(creds) is not None
    except ValueError as exc:
        logger.warning("[NEWRELIC-TOOL] Invalid stored credentials for user=%s: %s", user_id, exc)
        return False


def _get_client(user_id: str) -> tuple:
    """Return (client, error_json_or_None)."""
    creds = _get_stored_newrelic_credentials(user_id)
    if not creds:
        return None, json.dumps({"error": "New Relic not connected. Please connect New Relic first."})
    try:
        client = _build_client_from_creds(creds)
    except ValueError as exc:
        logger.warning("[NEWRELIC-TOOL] Invalid stored credentials for user=%s: %s", user_id, exc)
        return None, json.dumps({"error": "Stored New Relic credentials are invalid. Please reconnect."})
    if not client:
        return None, json.dumps({"error": "New Relic credentials are incomplete. Please reconnect."})
    return client, None


# ---------------------------------------------------------------------------
# Resource handlers
# ---------------------------------------------------------------------------

def _handle_nrql(client: NewRelicClient, query: str, time_range: str, limit: int) -> dict:
    """Execute a NRQL query and return structured results."""
    error = _validate_nrql(query)
    if error:
        return {"error": error}

    nrql = _inject_since_clause(query.strip(), time_range)
    nrql = _inject_limit_clause(nrql, min(limit, MAX_RESULTS_CAP))

    data = client.execute_nrql(nrql)
    results = data.get("results", [])
    metadata = data.get("metadata", {})

    facets = metadata.get("facets") or []
    event_types = metadata.get("eventTypes") or []
    time_window = metadata.get("timeWindow") or {}

    return {
        "resource_type": "nrql",
        "nrql": nrql,
        "count": len(results),
        "results": results,
        "facets": facets,
        "event_types": event_types,
        "time_window": time_window,
    }


def _handle_issues(client: NewRelicClient, query: str, time_range: str, limit: int) -> dict:
    """Fetch alert issues from NerdGraph."""
    from routes.newrelic.config import VALID_ISSUE_STATES
    states = None
    if query and query.strip():
        states = [s.strip().upper() for s in query.split(",") if s.strip()]
        invalid = [s for s in states if s not in VALID_ISSUE_STATES]
        if invalid:
            return {"error": f"Invalid issue state(s): {', '.join(invalid)}. Valid: {', '.join(sorted(VALID_ISSUE_STATES))}."}
    else:
        states = ["ACTIVATED", "CREATED"]

    data = client.get_issues(states=states, page_size=min(limit, MAX_RESULTS_CAP))
    issues = data.get("issues", [])

    return {
        "resource_type": "issues",
        "states_filter": states,
        "count": len(issues),
        "results": issues,
    }


def _handle_entities(client: NewRelicClient, query: str, time_range: str, limit: int) -> dict:
    """Search for monitored entities."""
    entity_type = None
    search_query = query.strip() if query else ""

    if "|" in search_query:
        parts = search_query.split("|", 1)
        search_query = parts[0].strip()
        entity_type = parts[1].strip() or None

    entities = client.search_entities(
        query_str=search_query,
        entity_type=entity_type,
        limit=min(limit, MAX_RESULTS_CAP),
    )

    return {
        "resource_type": "entities",
        "search_query": search_query,
        "entity_type_filter": entity_type,
        "count": len(entities),
        "results": entities,
    }


_HANDLERS: Dict[str, Any] = {
    "nrql": _handle_nrql,
    "issues": _handle_issues,
    "entities": _handle_entities,
}


# ---------------------------------------------------------------------------
# Main entry point (called by the LangChain agent)
# ---------------------------------------------------------------------------

def query_newrelic(
    resource_type: str,
    query: str = "",
    time_range: str = "1 hour",
    limit: int = 100,
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    """Query New Relic via NerdGraph for NRQL data, alert issues, or entity information.

    Returns a JSON string with the query results or an error message.
    """
    if not user_id:
        return json.dumps({"error": "User context not available"})

    client, err = _get_client(user_id)
    if err:
        return err

    resource_type = resource_type.lower().strip()
    handler = _HANDLERS.get(resource_type)
    if not handler:
        return json.dumps({
            "error": f"Invalid resource_type '{resource_type}'. Must be one of: {_RESOURCE_HELP}",
            "hint": "Use 'nrql' for any NRQL query (logs, metrics, transactions, errors, spans, infra).",
        })

    limit = min(max(limit, 1), MAX_RESULTS_CAP)
    logger.info(
        "[NEWRELIC-TOOL] user=%s resource=%s query=%s",
        user_id, resource_type, (query[:100] if query else ""),
    )

    try:
        result = handler(client, query, time_range, limit)

        if "error" in result:
            return json.dumps(result)

        result["success"] = True
        result["account_id"] = client.account_id
        result["region"] = client.region

        results_list = result.get("results", [])
        truncated_results, was_truncated = _truncate_results(results_list)
        if was_truncated:
            result["results"] = truncated_results
            result["truncated"] = True
            result["note"] = (
                f"Results truncated from {len(results_list)} to {len(truncated_results)} "
                "due to size limit. Use a more specific query or add LIMIT to narrow results."
            )
            result["count"] = len(truncated_results)

        return json.dumps(result, default=str)

    except NewRelicAPIError as exc:
        status = exc.status_code
        msg = str(exc)
        if status == 429:
            return json.dumps({"error": "New Relic API rate limit reached. Wait a moment and retry."})
        if status in (401, 403):
            return json.dumps({"error": "New Relic authentication failed. API key may be invalid or expired."})
        if exc.errors:
            gql_msgs = [e.get("message", "") for e in exc.errors]
            return json.dumps({"error": f"NerdGraph query error: {'; '.join(gql_msgs)}"})
        return json.dumps({"error": f"New Relic API error: {msg[:200]}"})
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    except Exception:
        logger.exception("[NEWRELIC-TOOL] Query failed for user=%s resource=%s", user_id, resource_type)
        return json.dumps({"error": "Internal error while querying New Relic"})
