"""Unified Datadog query tool for the RCA agent."""

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field

from routes.datadog.config import MAX_OUTPUT_SIZE, MAX_RESULTS_CAP
from routes.datadog.datadog_routes import (
    DatadogAPIError,
    _get_stored_datadog_credentials,
    _build_client_from_creds,
)

logger = logging.getLogger(__name__)


class QueryDatadogArgs(BaseModel):
    resource_type: str = Field(
        description="Type of data to query: 'logs', 'metrics', 'monitors', 'events', 'traces', 'hosts', or 'incidents'"
    )
    query: str = Field(
        default="",
        description="Search query. Syntax varies by resource type. "
        "Logs: 'service:web status:error'. Metrics: 'avg:system.cpu.user{*}'. "
        "Monitors: name filter. Events: source filter. "
        "Traces: 'service:web @http.status_code:500'. Hosts: host filter. "
        "Incidents: not used.",
    )
    time_from: str = Field(
        default="-1h",
        description="Start time: relative like '-1h', '-24h', '-7d' or ISO 8601",
    )
    time_to: str = Field(
        default="now",
        description="End time: 'now' or ISO 8601",
    )
    limit: int = Field(default=100, description="Maximum results to return")


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

_RELATIVE_RE = re.compile(r"^-(\d+)([mhdw])$")

_UNIT_MAP = {
    "m": "minutes",
    "h": "hours",
    "d": "days",
    "w": "weeks",
}


def _parse_relative_time(time_str: str) -> datetime:
    """Parse relative time strings like '-1h', '-30m', 'now', or ISO 8601."""
    stripped = time_str.strip().lower()
    if stripped == "now":
        return datetime.now(timezone.utc)

    m = _RELATIVE_RE.match(stripped)
    if m:
        amount = int(m.group(1))
        unit = _UNIT_MAP.get(m.group(2), "hours")
        return datetime.now(timezone.utc) - timedelta(**{unit: amount})

    try:
        dt = datetime.fromisoformat(time_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid time format: '{time_str}'. Use relative ('-1h', '-24h', '-7d'), 'now', or ISO 8601.") from exc


def _to_iso8601(time_str: str) -> str:
    """Convert a time string to ISO 8601 format."""
    dt = _parse_relative_time(time_str)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _to_unix_seconds(time_str: str) -> int:
    """Convert a time string to Unix timestamp in seconds."""
    return int(_parse_relative_time(time_str).timestamp())


def _to_unix_ms(time_str: str) -> int:
    """Convert a time string to Unix timestamp in milliseconds."""
    return int(_parse_relative_time(time_str).timestamp() * 1000)


# ---------------------------------------------------------------------------
# Connection helpers (reuse from datadog_routes to avoid logic drift)
# ---------------------------------------------------------------------------


def is_datadog_connected(user_id: str) -> bool:
    """Check if a user has valid Datadog credentials stored."""
    creds = _get_stored_datadog_credentials(user_id)
    return _build_client_from_creds(creds) is not None if creds else False


# ---------------------------------------------------------------------------
# Result truncation
# ---------------------------------------------------------------------------


def _truncate_results(results: list, serialized: list[str]) -> tuple[list, bool]:
    truncated, total_size = [], 0
    for item, item_str in zip(results, serialized):
        if total_size + len(item_str) > MAX_OUTPUT_SIZE:
            return truncated, True
        truncated.append(item)
        total_size += len(item_str)
    return truncated, False


# ---------------------------------------------------------------------------
# Resource-type handlers
# ---------------------------------------------------------------------------


def _query_logs(client, query: str, time_from: str, time_to: str, limit: int) -> dict:
    start = _to_iso8601(time_from)
    end = _to_iso8601(time_to)
    response = client.search_logs(query=query or "*", start=start, end=end, limit=limit)
    logs = response.get("data", [])[:limit]
    return {"resource_type": "logs", "count": len(logs), "results": logs}


def _query_metrics(client, query: str, time_from: str, time_to: str, limit: int) -> dict:
    if not query:
        raise ValueError("query is required for resource_type='metrics' (e.g., 'avg:system.cpu.user{*}')")
    start_ms = _to_unix_ms(time_from)
    end_ms = _to_unix_ms(time_to)
    response = client.query_metrics(query=query, start_ms=start_ms, end_ms=end_ms)
    data = response.get("data", {})
    attrs = data.get("attributes", {}) if isinstance(data, dict) else {}
    result_data = {
        "series": attrs.get("series", []),
        "times": attrs.get("times", []),
        "values": attrs.get("values", []),
    }
    return {"resource_type": "metrics", "count": len(result_data["series"]), "results": [result_data]}


def _query_monitors(client, query: str, time_from: str, time_to: str, limit: int) -> dict:
    params: dict[str, Any] = {"page_size": limit}
    if query:
        params["name"] = query
    monitors = client.list_monitors(params=params)
    if not isinstance(monitors, list):
        monitors = []
    return {"resource_type": "monitors", "count": len(monitors[:limit]), "results": monitors[:limit]}


def _query_events(client, query: str, time_from: str, time_to: str, limit: int) -> dict:
    start_ts = _to_unix_seconds(time_from)
    end_ts = _to_unix_seconds(time_to)
    params: dict[str, Any] = {}
    if query:
        params["sources"] = query
    response = client.list_events(start_ts=start_ts, end_ts=end_ts, params=params)
    events = response.get("events", [])[:limit]
    return {"resource_type": "events", "count": len(events), "results": events}


def _query_traces(client, query: str, time_from: str, time_to: str, limit: int) -> dict:
    start = _to_iso8601(time_from)
    end = _to_iso8601(time_to)
    response = client.search_traces(query=query or "*", start=start, end=end, limit=limit)
    spans = response.get("data", [])[:limit]
    return {"resource_type": "traces", "count": len(spans), "results": spans}


def _query_hosts(client, query: str, time_from: str, time_to: str, limit: int) -> dict:
    from_ts = _to_unix_seconds(time_from)
    response = client.list_hosts(query=query, count=limit, from_ts=from_ts)
    host_list = response.get("host_list", [])[:limit]
    return {"resource_type": "hosts", "count": len(host_list), "results": host_list}


def _query_incidents(client, query: str, time_from: str, time_to: str, limit: int) -> dict:
    response = client.list_incidents(page_size=limit)
    incidents = response.get("data", [])[:limit]
    return {"resource_type": "incidents", "count": len(incidents), "results": incidents}


_HANDLERS = {
    "logs": _query_logs,
    "metrics": _query_metrics,
    "monitors": _query_monitors,
    "events": _query_events,
    "traces": _query_traces,
    "hosts": _query_hosts,
    "incidents": _query_incidents,
}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def query_datadog(
    resource_type: str,
    query: str = "",
    time_from: str = "-1h",
    time_to: str = "now",
    limit: int = 100,
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    """Query Datadog for logs, metrics, monitors, events, traces, hosts, or incidents."""
    if not user_id:
        return json.dumps({"error": "User context not available"})

    creds = _get_stored_datadog_credentials(user_id)
    if not creds:
        return json.dumps({"error": "Datadog not connected. Please connect Datadog first."})

    client = _build_client_from_creds(creds)
    if not client:
        return json.dumps({"error": "Datadog credentials are incomplete. Please reconnect Datadog."})

    resource_type = resource_type.lower().strip()
    handler = _HANDLERS.get(resource_type)
    if not handler:
        return json.dumps({"error": f"Invalid resource_type '{resource_type}'. Must be one of: {', '.join(_HANDLERS)}"})

    limit = min(max(limit, 1), MAX_RESULTS_CAP)
    logger.info("[DATADOG-TOOL] user=%s resource=%s query=%s", user_id, resource_type, query[:100] if query else "")

    try:
        result = handler(client, query, time_from, time_to, limit)
        result["success"] = True
        result["time_range"] = f"{time_from} to {time_to}"

        results_list = result.get("results", [])
        serialized = [json.dumps(item) for item in results_list]
        truncated_results, was_truncated = _truncate_results(results_list, serialized)
        if was_truncated:
            result["results"] = truncated_results
            result["truncated"] = True
            result["note"] = f"Results truncated from {len(results_list)} to {len(truncated_results)} due to size limit."
            result["count"] = len(truncated_results)

        return json.dumps(result)

    except DatadogAPIError as exc:
        msg = str(exc)
        if "rate limit" in msg.lower():
            return json.dumps({"error": "Datadog API rate limit reached. Please retry later."})
        if "401" in msg or "403" in msg or "authentication" in msg.lower() or "forbidden" in msg.lower():
            return json.dumps({"error": "Datadog authentication failed. API key or app key may be invalid or expired."})
        return json.dumps({"error": f"Datadog API error: {msg[:200]}"})
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    except Exception as exc:
        logger.exception("[DATADOG-TOOL] Query failed for user=%s resource=%s", user_id, resource_type)
        return json.dumps({"error": "Internal error while querying Datadog"})
