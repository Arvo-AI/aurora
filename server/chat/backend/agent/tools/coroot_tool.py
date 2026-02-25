"""Coroot observability tools for the RCA agent.

Provides LLM-callable tools that query a user's Coroot instance for
incidents, application health, logs, traces, metrics, service maps,
deployments, and node status.  Credentials are loaded from Vault via
``get_token_data(user_id, "coroot")``.
"""

import functools
import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, Field

from connectors.coroot_connector.client import CorootAPIError, CorootClient, get_coroot_client
from utils.auth.token_management import get_token_data

logger = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 120000
MAX_LIST_ITEMS = 100
MAX_METRIC_DATAPOINTS = 120
LOOKBACK_HOURS_MAX = 720  # 30 days
MAX_LIMIT = 200
STATUS_LABELS = {0: "UNKNOWN", 1: "OK", 2: "INFO", 3: "WARNING", 4: "CRITICAL"}


# ---------------------------------------------------------------------------
# Credential helpers
# ---------------------------------------------------------------------------

def _get_coroot_credentials(user_id: str) -> Optional[Dict[str, Any]]:
    try:
        creds = get_token_data(user_id, "coroot")
        if not creds:
            return None
        if not creds.get("url") or not creds.get("email") or not creds.get("password"):
            return None
        return creds
    except Exception as exc:
        logger.error("[COROOT-TOOL] Failed to get credentials: %s", exc)
        return None


def _build_client(user_id: str) -> Optional[CorootClient]:
    creds = _get_coroot_credentials(user_id)
    if not creds:
        return None
    try:
        return get_coroot_client(
            user_id,
            url=creds["url"],
            email=creds["email"],
            password=creds["password"],
        )
    except CorootAPIError as exc:
        logger.error("[COROOT-TOOL] Failed to build client: %s", exc)
        return None
    except Exception as exc:
        logger.error("[COROOT-TOOL] Unexpected error building client: %s", exc)
        return None


def is_coroot_connected(user_id: str) -> bool:
    return _get_coroot_credentials(user_id) is not None


def _default_project(client: CorootClient) -> Optional[str]:
    """Return the first project id, or None."""
    try:
        projects = client.discover_projects()
        if projects:
            pid = projects[0].get("id")
            if pid:
                return str(pid)
    except CorootAPIError as exc:
        logger.error("[COROOT-TOOL] Failed to discover projects for default selection: %s", exc)
    except Exception as exc:
        logger.error("[COROOT-TOOL] Unexpected error discovering projects: %s", exc)
    return None


def _now_ts() -> int:
    return int(time.time())


def _clamp_lookback_hours(value: Any) -> int:
    """Coerce *value* to a numeric lookback and clamp to [0, LOOKBACK_HOURS_MAX].

    Non-numeric values fall back to a safe default of 1 hour.
    """
    try:
        hours = int(float(value))
    except (TypeError, ValueError):
        logger.warning("[COROOT-TOOL] Non-numeric lookback_hours=%r, defaulting to 1", value)
        return 1
    return max(0, min(hours, LOOKBACK_HOURS_MAX))


def _clamp_limit(value: Any, default: int = 50) -> int:
    """Coerce *value* to an integer in [1, MAX_LIMIT].

    Non-numeric values fall back to *default*.
    """
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        logger.warning("[COROOT-TOOL] Non-numeric limit=%r, defaulting to %d", value, default)
        return default
    return max(1, min(n, MAX_LIMIT))


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """Character-level safety net. Only used for detail endpoints where
    record-level truncation isn't practical. The returned envelope is
    valid JSON, but ``partial_data`` is a raw prefix of the original
    text and is NOT guaranteed to be syntactically valid JSON itself."""
    if len(text) <= limit:
        return text
    return json.dumps({
        "_truncated": True,
        "_message": (
            "Response exceeded size limit and was cut off. "
            "'partial_data' is a truncated raw text prefix and may not be valid JSON. "
            "Use filters or a shorter lookback_hours to narrow the query."
        ),
        "partial_data": text[:limit],
    })


def _safe_json(obj: Any) -> str:
    try:
        return _truncate(json.dumps(obj, indent=2, default=str))
    except (TypeError, ValueError):
        return _truncate(str(obj))


def _truncate_list(
    items: List[Any],
    total: int,
    max_items: int = MAX_LIST_ITEMS,
    hint: str = "Use filters or a shorter lookback_hours to narrow results.",
) -> tuple:
    """Truncate a list at the record level. Returns (items, metadata_dict).
    The metadata dict should be merged into the response so the agent knows
    what was cut and how to get the rest."""
    if len(items) <= max_items:
        return items, {}
    return items[:max_items], {
        "_truncated": True,
        "_returned": max_items,
        "_total": total,
        "_message": f"Showing {max_items} of {total} results. {hint}",
    }


def _trim_metric_datapoints(raw: Any) -> Any:
    """For panel/data results, keep only the last MAX_METRIC_DATAPOINTS per
    series so the agent gets recent data without being overwhelmed."""
    if not isinstance(raw, dict):
        return raw
    chart = raw.get("chart")
    if not isinstance(chart, dict):
        return raw
    series_list = chart.get("series")
    if not isinstance(series_list, list):
        return raw
    trimmed = False
    for series in series_list:
        if not isinstance(series, dict):
            continue
        values = series.get("data") or []
        if len(values) > MAX_METRIC_DATAPOINTS:
            original_len = len(values)
            series["data"] = values[-MAX_METRIC_DATAPOINTS:]
            series["_trimmed_datapoints"] = f"Showing last {MAX_METRIC_DATAPOINTS} of {original_len}. Use a shorter lookback_hours."
            trimmed = True
    if trimmed:
        raw["_note"] = "Some series were trimmed to most recent datapoints."
    return raw


def _status_label(code: Any) -> str:
    try:
        return STATUS_LABELS.get(int(code), str(code))
    except (TypeError, ValueError):
        return str(code)


def _extract_field(raw: Any, field: str) -> Any:
    """Extract a specific field from the Coroot unified response envelope.

    Coroot's overview endpoints return a full context object like:
        {"applications": [...], "map": {...}, "nodes": [...], "deployments": [...],
         "traces": {...}, "logs": {...}, "costs": {...}, "risks": [...],
         "categories": [...], "fluxcd": null}
    Only one field is populated; the rest are null.  This helper returns
    just the requested field so tool output is clean and token-efficient.
    """
    if isinstance(raw, dict):
        return raw.get(field)
    return raw


# ---------------------------------------------------------------------------
# Pydantic arg schemas
# ---------------------------------------------------------------------------

class CorootGetIncidentsArgs(BaseModel):
    lookback_hours: int = Field(default=24, description="How many hours back to search for incidents (default: 24)")
    project_id: Optional[str] = Field(default=None, description="Coroot project ID. Auto-detected if omitted.")


class CorootGetApplicationsArgs(BaseModel):
    lookback_hours: int = Field(default=1, description="Hours of data to consider (default: 1)")
    project_id: Optional[str] = Field(default=None, description="Coroot project ID. Auto-detected if omitted.")


class CorootGetAppDetailArgs(BaseModel):
    app_id: str = Field(description="Application ID (e.g. 'cluster:namespace:Deployment:name')")
    lookback_hours: int = Field(default=1, description="Hours of data to consider (default: 1)")
    project_id: Optional[str] = Field(default=None, description="Coroot project ID. Auto-detected if omitted.")


class CorootGetAppLogsArgs(BaseModel):
    app_id: str = Field(description="Application ID")
    severity: Optional[str] = Field(default=None, description="Filter by severity: Error, Warning, Info")
    message_filter: Optional[str] = Field(default=None, description="Substring or regex to filter log messages")
    source: str = Field(default="otel", description="Log source: 'otel' or 'agent'")
    limit: int = Field(default=50, description="Max log entries to return (default: 50)")
    lookback_hours: int = Field(default=1, description="Hours of data (default: 1)")
    project_id: Optional[str] = Field(default=None, description="Coroot project ID. Auto-detected if omitted.")


class CorootGetTracesArgs(BaseModel):
    service_name: Optional[str] = Field(default=None, description="Filter by service name")
    status_error: bool = Field(default=False, description="If true, only return error traces")
    trace_id: Optional[str] = Field(default=None, description="Look up a specific trace by ID")
    lookback_hours: int = Field(default=1, description="Hours of data (default: 1)")
    project_id: Optional[str] = Field(default=None, description="Coroot project ID. Auto-detected if omitted.")


class CorootGetServiceMapArgs(BaseModel):
    lookback_hours: int = Field(default=1, description="Hours of data (default: 1)")
    project_id: Optional[str] = Field(default=None, description="Coroot project ID. Auto-detected if omitted.")


class CorootQueryMetricsArgs(BaseModel):
    promql: str = Field(description="PromQL query (e.g. 'rate(container_resources_cpu_usage_seconds_total[5m])')")
    legend: str = Field(default="", description="Legend template using {{label}} syntax (e.g. '{{instance}}'). Leave empty for auto.")
    lookback_hours: int = Field(default=1, description="Hours of data (default: 1)")
    project_id: Optional[str] = Field(default=None, description="Coroot project ID. Auto-detected if omitted.")


class CorootGetDeploymentsArgs(BaseModel):
    lookback_hours: int = Field(default=24, description="Hours of data (default: 24)")
    project_id: Optional[str] = Field(default=None, description="Coroot project ID. Auto-detected if omitted.")


class CorootGetIncidentDetailArgs(BaseModel):
    incident_key: str = Field(description="Unique incident key from the incidents list")
    lookback_hours: int = Field(default=6, description="Hours of context around incident (default: 6)")
    project_id: Optional[str] = Field(default=None, description="Coroot project ID. Auto-detected if omitted.")


class CorootGetNodesArgs(BaseModel):
    lookback_hours: int = Field(default=1, description="Hours of data (default: 1)")
    project_id: Optional[str] = Field(default=None, description="Coroot project ID. Auto-detected if omitted.")


class CorootGetOverviewLogsArgs(BaseModel):
    severity: Optional[str] = Field(default=None, description="Filter by severity: Error, Warning, Info")
    message_filter: Optional[str] = Field(default=None, description="Substring or regex to filter log messages")
    kubernetes_only: bool = Field(default=False, description="If true, return only Kubernetes events (OOMKilled, Evicted, CrashLoopBackOff, FailedScheduling). If false (default), return application logs. Call twice to get both.")
    limit: int = Field(default=50, description="Max log entries to return (default: 50)")
    lookback_hours: int = Field(default=1, description="Hours of data (default: 1)")
    project_id: Optional[str] = Field(default=None, description="Coroot project ID. Auto-detected if omitted.")


class CorootGetNodeDetailArgs(BaseModel):
    node_name: str = Field(description="Node name (from coroot_get_nodes)")
    lookback_hours: int = Field(default=1, description="Hours of data (default: 1)")
    project_id: Optional[str] = Field(default=None, description="Coroot project ID. Auto-detected if omitted.")


class CorootGetCostsArgs(BaseModel):
    lookback_hours: int = Field(default=24, description="Hours of data (default: 24)")
    project_id: Optional[str] = Field(default=None, description="Coroot project ID. Auto-detected if omitted.")


class CorootGetRisksArgs(BaseModel):
    lookback_hours: int = Field(default=1, description="Hours of data (default: 1)")
    project_id: Optional[str] = Field(default=None, description="Coroot project ID. Auto-detected if omitted.")


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _resolve_project(client: CorootClient, project_id: Optional[str]) -> Optional[str]:
    if project_id:
        return project_id
    return _default_project(client)


def _coroot_tool(fn: Callable[..., str]) -> Callable[..., str]:
    """Decorator that centralizes the boilerplate shared by every Coroot tool.

    Handles: user_id validation, client construction, project resolution,
    lookback clamping, timestamp window computation, and CorootAPIError /
    generic exception wrapping.

    The decorated function receives ``(client, pid, from_ts, to_ts, **kwargs)``
    and only needs to implement the API call + response formatting.
    """

    @functools.wraps(fn)
    def wrapper(
        *,
        user_id: Optional[str] = None,
        lookback_hours: int = 1,
        project_id: Optional[str] = None,
        **kwargs: Any,
    ) -> str:
        if not user_id:
            return json.dumps({"error": "User context not available"})

        lookback_hours = _clamp_lookback_hours(lookback_hours)

        client = _build_client(user_id)
        if not client:
            return json.dumps({"error": "Coroot not connected. Ask the user to connect Coroot first."})

        pid = _resolve_project(client, project_id)
        if not pid:
            return json.dumps({"error": "No Coroot project found. Check the connection."})

        to_ts = _now_ts()
        from_ts = to_ts - (lookback_hours * 3600)

        try:
            return fn(client=client, pid=pid, from_ts=from_ts, to_ts=to_ts,
                      lookback_hours=lookback_hours, **kwargs)
        except CorootAPIError as exc:
            return json.dumps({"error": f"Coroot API error: {exc}"})
        except Exception as exc:
            logger.error("[COROOT-TOOL] %s failed: %s", fn.__name__, exc)
            return json.dumps({"error": f"Unexpected error contacting Coroot: {exc}"})

    return wrapper


@_coroot_tool
def coroot_get_incidents(
    *,
    client: CorootClient,
    pid: str,
    from_ts: int,
    to_ts: int,
    lookback_hours: int = 24,
    **kwargs,
) -> str:
    """List recent incidents from Coroot with RCA summaries."""
    raw = client.get_incidents(pid, from_ts, to_ts)

    if not raw:
        return json.dumps({"incidents": [], "message": f"No incidents in the last {lookback_hours}h"})

    summaries = []
    for inc in raw:
        rca = inc.get("rca") or {}
        summary = {
            "key": inc.get("key"),
            "application_id": inc.get("application_id"),
            "severity": _status_label(inc.get("severity")),
            "opened_at": inc.get("opened_at"),
            "resolved_at": inc.get("resolved_at"),
            "duration_s": inc.get("duration"),
            "short_description": inc.get("short_description"),
            "root_cause": rca.get("root_cause"),
            "immediate_fixes": rca.get("immediate_fixes"),
            "impact": inc.get("impact"),
        }
        summaries.append(summary)

    summaries.sort(key=lambda x: x.get("opened_at") or 0, reverse=True)
    open_count = sum(1 for s in summaries if not s.get("resolved_at"))

    items, trunc_meta = _truncate_list(
        summaries, len(summaries),
        hint="Use a shorter lookback_hours to narrow results.",
    )

    result = {
        "total_incidents": len(summaries),
        "open_incidents": open_count,
        "lookback_hours": lookback_hours,
        "incidents": items,
    }
    result.update(trunc_meta)
    return _safe_json(result)


@_coroot_tool
def coroot_get_incident_detail(
    *,
    client: CorootClient,
    pid: str,
    from_ts: int,
    to_ts: int,
    incident_key: str,
    lookback_hours: int = 6,
    **kwargs,
) -> str:
    """Get full detail for a specific incident including RCA and propagation map."""
    raw = client.get_incident_detail(pid, incident_key, from_ts, to_ts)

    if not raw:
        return json.dumps({
            "incident_key": incident_key,
            "message": f"No detail found for incident '{incident_key}'. It may have been resolved and aged out.",
        })

    return _safe_json(raw)


@_coroot_tool
def coroot_get_applications(
    *,
    client: CorootClient,
    pid: str,
    from_ts: int,
    to_ts: int,
    lookback_hours: int = 1,
    **kwargs,
) -> str:
    """List all applications with health status from Coroot."""
    raw = client.get_applications(pid, from_ts, to_ts)

    app_list = _extract_field(raw, "applications")
    if not app_list:
        return json.dumps({"applications": [], "message": "No applications found"})

    results = []
    for app in app_list:
        entry: Dict[str, Any] = {
            "id": app.get("id"),
            "status": _status_label(app.get("status")),
        }
        for field in ("errors", "latency", "upstreams", "instances", "restarts",
                       "cpu", "memory", "network", "dns", "logs"):
            val = app.get(field)
            if isinstance(val, dict):
                entry[field] = f"{_status_label(val.get('status'))}:{val.get('value', '')}"
            elif val is not None:
                entry[field] = val
        results.append(entry)

    critical = sum(1 for r in results if "CRITICAL" in str(r.get("status", "")))
    warning = sum(1 for r in results if "WARNING" in str(r.get("status", "")))

    items, trunc_meta = _truncate_list(
        results, len(results),
        hint="All applications are returned sorted by status. Filter by status in follow-up queries.",
    )

    result = {
        "total_applications": len(results),
        "critical": critical,
        "warning": warning,
        "applications": items,
    }
    result.update(trunc_meta)
    return _safe_json(result)


@_coroot_tool
def coroot_get_app_detail(
    *,
    client: CorootClient,
    pid: str,
    from_ts: int,
    to_ts: int,
    app_id: str,
    lookback_hours: int = 1,
    **kwargs,
) -> str:
    """Get full audit reports for one application (SLO, CPU, Memory, Net, Logs, DB, etc.)."""
    raw = client.get_app_detail(pid, app_id, from_ts, to_ts)

    result: Dict[str, Any] = {"app_id": app_id}

    app_map = raw.get("app_map") if isinstance(raw, dict) else None
    if app_map:
        app_info = app_map.get("application") or {}
        result["status"] = _status_label(app_info.get("status"))
        result["indicators"] = app_info.get("indicators") or []
        result["instances"] = [
            {"id": i.get("id"), "labels": i.get("labels")}
            for i in (app_map.get("instances") or [])
        ]
        result["clients"] = [
            {"id": c.get("id"), "status": _status_label(c.get("link_status")), "stats": c.get("link_stats")}
            for c in (app_map.get("clients") or [])
        ]
        result["dependencies"] = [
            {"id": d.get("id"), "status": _status_label(d.get("link_status")), "stats": d.get("link_stats")}
            for d in (app_map.get("dependencies") or [])
        ]

    reports = (raw.get("reports") or []) if raw else []
    failing_checks = []
    for report in reports:
        for check in (report.get("checks") or []):
            status = check.get("status", 0)
            try:
                status_int = int(status)
            except (TypeError, ValueError):
                status_int = 0
            if status_int >= 3:
                failing_checks.append({
                    "report": report.get("name"),
                    "check_id": check.get("id"),
                    "title": check.get("title"),
                    "status": _status_label(status),
                    "message": check.get("message"),
                })
    result["failing_checks"] = failing_checks
    result["total_reports"] = len(reports)

    return _safe_json(result)


@_coroot_tool
def coroot_get_app_logs(
    *,
    client: CorootClient,
    pid: str,
    from_ts: int,
    to_ts: int,
    app_id: str,
    severity: Optional[str] = None,
    message_filter: Optional[str] = None,
    source: str = "otel",
    limit: int = 50,
    lookback_hours: int = 1,
    **kwargs,
) -> str:
    """Fetch logs for a SINGLE application (requires app_id). Use coroot_get_overview_logs for cluster-wide search."""
    limit = _clamp_limit(limit, default=50)

    query: Dict[str, Any] = {
        "source": source,
        "view": "messages",
        "filters": [],
        "limit": limit,
    }
    if severity:
        query["filters"].append({"name": "Severity", "op": "=", "value": severity})
    if message_filter:
        query["filters"].append({"name": "Message", "op": "=", "value": message_filter})

    raw = client.get_app_logs(pid, app_id, from_ts, to_ts, query)

    if not isinstance(raw, dict):
        return json.dumps({
            "app_id": app_id,
            "source": source,
            "total_entries": 0,
            "entries": [],
            "message": f"No log entries found for {app_id} (source={source}) in the last {lookback_hours}h.",
        })

    entries = []
    for e in (raw.get("entries") or []):
        entries.append({
            "timestamp": e.get("timestamp"),
            "severity": e.get("severity"),
            "message": e.get("message"),
            "trace_id": e.get("trace_id"),
            "attributes": e.get("attributes"),
        })

    items, trunc_meta = _truncate_list(
        entries, len(entries),
        hint="Use severity/message_filter to narrow, or reduce lookback_hours/limit.",
    )

    result = {
        "app_id": app_id,
        "source": raw.get("source", source),
        "total_entries": len(entries),
        "entries": items,
    }
    if not entries:
        result["message"] = f"No log entries found for {app_id} (source={source}) in the last {lookback_hours}h."
    result.update(trunc_meta)
    return _safe_json(result)


@_coroot_tool
def coroot_get_traces(
    *,
    client: CorootClient,
    pid: str,
    from_ts: int,
    to_ts: int,
    service_name: Optional[str] = None,
    status_error: bool = False,
    trace_id: Optional[str] = None,
    lookback_hours: int = 1,
    **kwargs,
) -> str:
    """Search traces across all applications or look up a trace by ID."""
    if trace_id:
        query: Dict[str, Any] = {"trace_id": trace_id}
    else:
        filters = []
        if service_name:
            filters.append({"field": "ServiceName", "op": "=", "value": service_name})
        if status_error:
            filters.append({"field": "StatusCode", "op": "=", "value": "STATUS_CODE_ERROR"})
        query = {"view": "traces", "filters": filters}

    raw = client.get_traces(pid, from_ts, to_ts, query)

    traces_data = _extract_field(raw, "traces")
    if not traces_data:
        filters_desc = f"service={service_name}" if service_name else "all services"
        if status_error:
            filters_desc += ", errors only"
        if trace_id:
            filters_desc = f"trace_id={trace_id}"
        return json.dumps({
            "traces": [],
            "filters": filters_desc,
            "lookback_hours": lookback_hours,
            "message": f"No traces found for {filters_desc} in the last {lookback_hours}h.",
        })

    return _safe_json(traces_data)


@_coroot_tool
def coroot_get_service_map(
    *,
    client: CorootClient,
    pid: str,
    from_ts: int,
    to_ts: int,
    lookback_hours: int = 1,
    **kwargs,
) -> str:
    """Get the service dependency map showing all applications and their connections."""
    raw = client.get_service_map(pid, from_ts, to_ts)

    map_data = _extract_field(raw, "map")
    services: List[Dict] = map_data if isinstance(map_data, list) else []
    if not services:
        return json.dumps({"services": [], "message": "No service map data"})

    result = []
    for svc in services:
        entry: Dict[str, Any] = {
            "id": svc.get("id"),
            "status": _status_label(svc.get("status")),
        }
        ups = svc.get("upstreams") or []
        if ups:
            entry["upstreams"] = [
                {"id": u.get("id"), "status": _status_label(u.get("status")), "stats": u.get("stats")}
                for u in ups
            ]
        downs = svc.get("downstreams") or []
        if downs:
            entry["downstreams"] = [d.get("id") for d in downs]
        result.append(entry)

    items, trunc_meta = _truncate_list(
        result, len(result),
        hint="Service map is large. Focus investigation on specific app_ids.",
    )

    out = {"total_services": len(result), "services": items}
    out.update(trunc_meta)
    return _safe_json(out)


@_coroot_tool
def coroot_query_metrics(
    *,
    client: CorootClient,
    pid: str,
    from_ts: int,
    to_ts: int,
    promql: str,
    legend: str = "",
    lookback_hours: int = 1,
    **kwargs,
) -> str:
    """Execute a PromQL query against Coroot's panel/data endpoint."""
    raw = client.query_panel_data(pid, promql, from_ts, to_ts, legend)

    if not raw:
        return json.dumps({
            "query": promql,
            "lookback_hours": lookback_hours,
            "series": [],
            "message": f"No data returned for query '{promql}' over the last {lookback_hours}h.",
        })

    chart = raw.get("chart") or {}
    if not chart.get("series"):
        return json.dumps({
            "query": promql,
            "lookback_hours": lookback_hours,
            "series": [],
            "message": f"Query '{promql}' returned no time-series data for the last {lookback_hours}h.",
        })

    raw = _trim_metric_datapoints(raw)
    return _safe_json(raw)


@_coroot_tool
def coroot_get_deployments(
    *,
    client: CorootClient,
    pid: str,
    from_ts: int,
    to_ts: int,
    lookback_hours: int = 24,
    **kwargs,
) -> str:
    """List recent deployments to correlate with incidents."""
    raw = client.get_deployments(pid, from_ts, to_ts)

    deployments = _extract_field(raw, "deployments")
    if not deployments:
        return json.dumps({
            "deployments": [],
            "lookback_hours": lookback_hours,
            "message": f"No deployments detected in the last {lookback_hours}h.",
        })

    items, trunc_meta = _truncate_list(
        deployments, len(deployments),
        hint="Use a shorter lookback_hours to narrow results.",
    )
    result = {"total_deployments": len(deployments), "deployments": items}
    result.update(trunc_meta)
    return _safe_json(result)


@_coroot_tool
def coroot_get_nodes(
    *,
    client: CorootClient,
    pid: str,
    from_ts: int,
    to_ts: int,
    lookback_hours: int = 1,
    **kwargs,
) -> str:
    """List all nodes with health status (CPU, memory, disk)."""
    raw = client.get_nodes(pid, from_ts, to_ts)

    nodes = _extract_field(raw, "nodes")
    if not nodes:
        return json.dumps({
            "nodes": [],
            "message": "No nodes found. Coroot's node-agent may not be installed.",
        })

    items, trunc_meta = _truncate_list(
        nodes, len(nodes),
        hint="Use filters or a shorter lookback_hours to narrow results.",
    )
    result = {"total_nodes": len(nodes), "nodes": items}
    result.update(trunc_meta)
    return _safe_json(result)


@_coroot_tool
def coroot_get_overview_logs(
    *,
    client: CorootClient,
    pid: str,
    from_ts: int,
    to_ts: int,
    severity: Optional[str] = None,
    message_filter: Optional[str] = None,
    kubernetes_only: bool = False,
    limit: int = 50,
    lookback_hours: int = 1,
    **kwargs,
) -> str:
    """Search logs across ALL applications at once (no app_id needed), or query Kubernetes events. Use coroot_get_app_logs when you already know the app."""
    limit = _clamp_limit(limit, default=50)

    query: Dict[str, Any] = {
        "agent": True,
        "otel": True,
        "filters": [],
        "limit": limit,
    }
    if severity:
        query["filters"].append({"name": "Severity", "op": "=", "value": severity})
    if message_filter:
        query["filters"].append({"name": "Message", "op": "=", "value": message_filter})
    if kubernetes_only:
        query["filters"].append({"name": "service.name", "op": "=", "value": "KubernetesEvents"})

    raw = client.get_overview_logs(pid, from_ts, to_ts, query)

    if not isinstance(raw, dict):
        return json.dumps({
            "total_entries": 0,
            "entries": [],
            "message": f"No log entries found in the last {lookback_hours}h.",
        })

    entries = []
    for e in (raw.get("entries") or []):
        entries.append({
            "timestamp": e.get("timestamp"),
            "severity": e.get("severity"),
            "message": e.get("message"),
            "application": e.get("application"),
            "cluster": e.get("cluster"),
            "trace_id": e.get("trace_id"),
            "attributes": e.get("attributes"),
        })

    items, trunc_meta = _truncate_list(
        entries, len(entries),
        hint="Use severity/message_filter to narrow, or reduce lookback_hours/limit.",
    )

    result = {
        "total_entries": len(entries),
        "entries": items,
    }
    if not entries:
        result["message"] = f"No log entries found in the last {lookback_hours}h."
    result.update(trunc_meta)
    return _safe_json(result)


@_coroot_tool
def coroot_get_node_detail(
    *,
    client: CorootClient,
    pid: str,
    from_ts: int,
    to_ts: int,
    node_name: str,
    lookback_hours: int = 1,
    **kwargs,
) -> str:
    """Get full audit report for a specific node (CPU, memory, disk, network, GPU)."""
    raw = client.get_node_detail(pid, node_name, from_ts, to_ts)

    if not raw:
        return json.dumps({
            "node_name": node_name,
            "message": f"No data found for node '{node_name}'. Verify the name via coroot_get_nodes.",
        })

    return _safe_json(raw)


@_coroot_tool
def coroot_get_costs(
    *,
    client: CorootClient,
    pid: str,
    from_ts: int,
    to_ts: int,
    lookback_hours: int = 24,
    **kwargs,
) -> str:
    """Get cost breakdown per node and per application, plus right-sizing recommendations."""
    raw = client.get_costs(pid, from_ts, to_ts)

    costs_data = _extract_field(raw, "costs")
    if not costs_data:
        return json.dumps({
            "costs": [],
            "message": "No cost data available. Cloud pricing integration may not be configured in Coroot.",
        })

    return _safe_json(costs_data)


@_coroot_tool
def coroot_get_risks(
    *,
    client: CorootClient,
    pid: str,
    from_ts: int,
    to_ts: int,
    lookback_hours: int = 1,
    **kwargs,
) -> str:
    """Get security and availability risks (single-instance, single-AZ, exposed ports, spot-only)."""
    raw = client.get_risks(pid, from_ts, to_ts)

    risks_data = _extract_field(raw, "risks")
    if not risks_data:
        return json.dumps({
            "risks": [],
            "message": "No risks detected or risk analysis is not available for this project.",
        })

    items, trunc_meta = _truncate_list(
        risks_data, len(risks_data),
        hint="Use filters or a shorter lookback_hours to narrow results.",
    )
    result = {"total_risks": len(risks_data), "risks": items}
    result.update(trunc_meta)
    return _safe_json(result)
