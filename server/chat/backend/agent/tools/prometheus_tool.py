"""Prometheus PromQL query tool for the RCA agent.

Supports instant queries, range queries, firing alerts, alerting rules,
scrape target health, and metric metadata — all via the Prometheus HTTP API.
"""

import json
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from connectors.prometheus_connector.client import PrometheusClient, PrometheusAPIError
from routes.prometheus.prometheus_routes import _get_stored_prometheus_credentials

logger = logging.getLogger(__name__)

MAX_OUTPUT_SIZE = 50_000
MAX_RESULTS_CAP = 200

_VALID_RESOURCE_TYPES = (
    "metrics",
    "instant",
    "alerts",
    "rules",
    "targets",
    "metadata",
)

_RESOURCE_HELP = ", ".join(f"'{r}'" for r in _VALID_RESOURCE_TYPES)


class QueryPrometheusArgs(BaseModel):
    resource_type: str = Field(
        description=(
            "Type of query to run. One of: "
            "'metrics' — execute a PromQL range query over a time window (returns time series). "
            "'instant' — execute a PromQL instant query (single point-in-time snapshot). "
            "'alerts' — list currently firing alerts from Prometheus alerting rules. "
            "'rules' — list alerting and recording rules configured in Prometheus. "
            "'targets' — list scrape targets and their health status (UP/DOWN). "
            "'metadata' — get metric metadata (type, help text, unit) for available metrics."
        )
    )
    query: str = Field(
        default="",
        description=(
            "PromQL expression or filter. Meaning depends on resource_type:\n"
            "  metrics/instant: A PromQL query, e.g. 'rate(http_requests_total{status=~\"5..\"}[5m])'\n"
            "  alerts: Not used (returns all firing alerts)\n"
            "  rules: Not used (returns all rules)\n"
            "  targets: Optional state filter: 'active', 'dropped', or 'any'\n"
            "  metadata: Optional metric name to filter, e.g. 'http_requests_total'"
        ),
    )
    time_from: str = Field(
        default="1h",
        description=(
            "Start of time range (for range queries). Relative: '1h', '30m', '24h', '7d'. "
            "Or ISO 8601 timestamp. Defaults to 1 hour ago."
        ),
    )
    time_to: str = Field(
        default="now",
        description=(
            "End of time range (for range queries). 'now' or ISO 8601 timestamp. Defaults to now."
        ),
    )
    step: str = Field(
        default="60s",
        description="Resolution step for range queries (e.g. '15s', '60s', '5m'). Defaults to 60s.",
    )
    limit: int = Field(
        default=100,
        description="Maximum number of results/series to return (default: 100).",
    )


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

_RELATIVE_RE = re.compile(r"^(\d+)\s*(s(?:ec(?:ond)?s?)?|m(?:in(?:ute)?s?)?|h(?:ours?)?|d(?:ays?)?|w(?:eeks?)?)$", re.IGNORECASE)

_UNIT_SECONDS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "week": 604800, "weeks": 604800,
}


def _parse_time(time_str: str) -> str:
    """Convert a time string to a Unix timestamp or pass through ISO format."""
    stripped = time_str.strip().lower()

    if stripped == "now":
        return str(datetime.now(timezone.utc).timestamp())

    # Relative time (e.g. "1h", "30m", "7d")
    m = _RELATIVE_RE.match(stripped)
    if m:
        amount = int(m.group(1))
        unit_key = m.group(2).rstrip("s").lower()
        seconds = _UNIT_SECONDS.get(unit_key, 3600) * amount
        ts = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        return str(ts.timestamp())

    # Try ISO 8601
    try:
        dt = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
        return str(dt.timestamp())
    except ValueError:
        pass

    # Default to 1 hour ago
    ts = datetime.now(timezone.utc) - timedelta(hours=1)
    return str(ts.timestamp())


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
        if total_size + item_len > max_size:
            return truncated, True
        truncated.append(item)
        total_size += item_len
    return truncated, False


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

def is_prometheus_connected(user_id: str) -> bool:
    """Check if a user has valid Prometheus credentials stored."""
    client, _ = _get_client(user_id)
    return client is not None


def _get_client(user_id: str) -> tuple:
    """Return (client, error_json_or_None)."""
    creds = _get_stored_prometheus_credentials(user_id)
    if not creds:
        return None, json.dumps({"error": "Prometheus not connected. Please connect Prometheus first."})

    prometheus_url = creds.get("prometheus_url")
    if not prometheus_url:
        return None, json.dumps({"error": "Prometheus credentials are incomplete. Please reconnect."})

    try:
        client = PrometheusClient(
            prometheus_url=prometheus_url,
            auth_type=creds.get("auth_type", "none"),
            username=creds.get("username"),
            password=creds.get("password"),
            bearer_token=creds.get("bearer_token"),
            custom_headers=creds.get("custom_headers"),
            verify_ssl=creds.get("verify_ssl", True),
        )
    except ValueError as exc:
        logger.warning("[PROMETHEUS-TOOL] Invalid stored credentials for user=%s: %s", user_id, exc)
        return None, json.dumps({"error": "Stored Prometheus credentials are invalid. Please reconnect."})

    return client, None


# ---------------------------------------------------------------------------
# Resource handlers
# ---------------------------------------------------------------------------

def _handle_metrics(client: PrometheusClient, query: str, time_from: str, time_to: str, step: str, limit: int) -> dict:
    """Execute a PromQL range query and return time series data."""
    if not query or not query.strip():
        return {"error": "A PromQL query is required for resource_type='metrics'. Example: rate(http_requests_total[5m])"}

    start = _parse_time(time_from)
    end = _parse_time(time_to)

    data = client.query_range(promql=query.strip(), start=start, end=end, step=step)

    results = data.get("result", [])
    result_type = data.get("resultType", "matrix")

    return {
        "resource_type": "metrics",
        "query": query.strip(),
        "time_from": time_from,
        "time_to": time_to,
        "step": step,
        "resultType": result_type,
        "count": len(results),
        "results": results[:limit],
        "truncated_by_limit": len(results) > limit,
    }


def _handle_instant(client: PrometheusClient, query: str, time_from: str, time_to: str, step: str, limit: int) -> dict:
    """Execute a PromQL instant query (point-in-time)."""
    if not query or not query.strip():
        return {"error": "A PromQL query is required for resource_type='instant'. Example: up{job='prometheus'}"}

    # Use time_to as the evaluation time (default: now)
    eval_time = _parse_time(time_to) if time_to != "now" else None

    data = client.query_instant(promql=query.strip(), time=eval_time)

    results = data.get("result", [])
    result_type = data.get("resultType", "vector")

    return {
        "resource_type": "instant",
        "query": query.strip(),
        "resultType": result_type,
        "count": len(results),
        "results": results[:limit],
        "truncated_by_limit": len(results) > limit,
    }


def _handle_alerts(client: PrometheusClient, query: str, time_from: str, time_to: str, step: str, limit: int) -> dict:
    """Get currently firing alerts."""
    alerts = client.get_alerts()

    return {
        "resource_type": "alerts",
        "count": len(alerts),
        "results": alerts[:limit],
    }


def _handle_rules(client: PrometheusClient, query: str, time_from: str, time_to: str, step: str, limit: int) -> dict:
    """Get alerting and recording rules."""
    # query can optionally be 'alert' or 'record' to filter
    rule_type = query.strip().lower() if query and query.strip() in ("alert", "record") else None
    groups = client.get_rules(rule_type=rule_type)

    return {
        "resource_type": "rules",
        "rule_type_filter": rule_type,
        "group_count": len(groups),
        "results": groups[:limit],
    }


def _handle_targets(client: PrometheusClient, query: str, time_from: str, time_to: str, step: str, limit: int) -> dict:
    """Get scrape targets and their health status."""
    state_filter = query.strip().lower() if query and query.strip() in ("active", "dropped", "any") else None
    data = client.get_targets(state=state_filter)

    active_targets = data.get("activeTargets", [])
    dropped_targets = data.get("droppedTargets", [])

    # Summarize target health
    health_summary = {}
    for t in active_targets:
        health = t.get("health", "unknown")
        health_summary[health] = health_summary.get(health, 0) + 1

    return {
        "resource_type": "targets",
        "state_filter": state_filter,
        "active_count": len(active_targets),
        "dropped_count": len(dropped_targets),
        "health_summary": health_summary,
        "results": active_targets[:limit],
    }


def _handle_metadata(client: PrometheusClient, query: str, time_from: str, time_to: str, step: str, limit: int) -> dict:
    """Get metric metadata (type, help, unit)."""
    metric_filter = query.strip() if query and query.strip() else None
    data = client.get_metadata(metric=metric_filter, limit=limit)

    # data is a dict of {metric_name: [metadata_entries]}
    flat_results = []
    for metric_name, entries in data.items():
        for entry in entries:
            flat_results.append({
                "metric": metric_name,
                "type": entry.get("type"),
                "help": entry.get("help"),
                "unit": entry.get("unit"),
            })

    return {
        "resource_type": "metadata",
        "metric_filter": metric_filter,
        "count": len(flat_results),
        "results": flat_results[:limit],
    }


_HANDLERS: Dict[str, Any] = {
    "metrics": _handle_metrics,
    "instant": _handle_instant,
    "alerts": _handle_alerts,
    "rules": _handle_rules,
    "targets": _handle_targets,
    "metadata": _handle_metadata,
}


# ---------------------------------------------------------------------------
# Main entry point (called by the LangChain agent)
# ---------------------------------------------------------------------------

def query_prometheus(
    resource_type: str,
    query: str = "",
    time_from: str = "1h",
    time_to: str = "now",
    step: str = "60s",
    limit: int = 100,
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    """Query Prometheus for metrics, alerts, targets, rules, or metadata.

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
            "hint": "Use 'metrics' for PromQL range queries, 'instant' for point-in-time queries, "
                    "'alerts' for firing alerts, 'targets' to check scrape health.",
        })

    limit = min(max(limit, 1), MAX_RESULTS_CAP)
    logger.info(
        "[PROMETHEUS-TOOL] user=%s resource=%s query=%s",
        user_id, resource_type, (query[:100] if query else ""),
    )

    try:
        result = handler(client, query, time_from, time_to, step, limit)

        if "error" in result:
            return json.dumps(result)

        result["success"] = True
        result["prometheus_url"] = client.base_url

        results_list = result.get("results", [])
        truncated_results, was_truncated = _truncate_results(results_list)
        if was_truncated:
            result["results"] = truncated_results
            result["truncated"] = True
            result["note"] = (
                f"Results truncated from {len(results_list)} to {len(truncated_results)} "
                "due to size limit. Use a more specific PromQL query to narrow results."
            )
            result["count"] = len(truncated_results)

        return json.dumps(result, default=str)

    except PrometheusAPIError as exc:
        status = exc.status_code
        msg = str(exc)
        if status == 429:
            return json.dumps({"error": "Prometheus rate limit reached. Wait a moment and retry."})
        if status in (401, 403):
            return json.dumps({"error": "Prometheus authentication failed. Credentials may be invalid or expired."})
        return json.dumps({"error": f"Prometheus API error: {msg[:300]}"})
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    except Exception:
        logger.exception("[PROMETHEUS-TOOL] Query failed for user=%s resource=%s", user_id, resource_type)
        return json.dumps({"error": "Internal error while querying Prometheus"})
