"""Unified Datadog query tool for the RCA agent."""

import json
import logging
import math
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
        description="Type of data to query: 'logs', 'metrics', 'metric_stats', 'monitors', "
        "'events', 'traces', 'hosts', or 'incidents'"
    )
    query: str = Field(
        default="",
        description="Search query. Syntax varies by resource type. "
        "Logs: 'service:web status:error'. Metrics: 'avg:system.cpu.user{*}'. "
        "Metric_stats: same metric syntax as metrics, grouping encouraged "
        "e.g. 'sum:kubernetes.memory.usage{env:production} by {kube_deployment}'. "
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
    interval: Optional[int] = Field(
        default=None,
        description="Rollup granularity in MILLISECONDS for 'metrics' and 'metric_stats'. "
        "Clamped to 60000..14400000. For 'metric_stats', omit it to auto-pick a "
        "granularity that keeps each series under ~1000 points (a 30-day window "
        "auto-picks 1h). For 'metrics', omitting it leaves Datadog's own default "
        "resolution in place.",
    )


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


def _query_logs(client, query: str, time_from: str, time_to: str, limit: int, **kwargs) -> dict:
    start = _to_iso8601(time_from)
    end = _to_iso8601(time_to)
    response = client.search_logs(query=query or "*", start=start, end=end, limit=limit)
    logs = response.get("data", [])[:limit]
    return {"resource_type": "logs", "count": len(logs), "results": logs}


def _query_metrics(client, query: str, time_from: str, time_to: str, limit: int, **kwargs) -> dict:
    if not query:
        raise ValueError("query is required for resource_type='metrics' (e.g., 'avg:system.cpu.user{*}')")
    start_ms = _to_unix_ms(time_from)
    end_ms = _to_unix_ms(time_to)
    # Honour an explicit interval, but do NOT auto-pick one here. Datadog's own
    # default resolution is part of this handler's existing output contract, which
    # the RCA prompts and SKILL.md depend on; changing it is out of scope for
    # right-sizing. metric_stats auto-picks because it is new and owns its own
    # contract.
    interval = _clamp_interval(kwargs.get("interval"))
    response = client.query_metrics(query=query, start_ms=start_ms, end_ms=end_ms, interval=interval)
    data = response.get("data", {})
    attrs = data.get("attributes", {}) if isinstance(data, dict) else {}
    result_data = {
        "series": attrs.get("series", []),
        "times": attrs.get("times", []),
        "values": attrs.get("values", []),
    }
    return {"resource_type": "metrics", "count": len(result_data["series"]), "results": [result_data]}


# ---------------------------------------------------------------------------
# metric_stats: server-side percentile summaries
#
# Datadog has NO time-percentile capability. Every documented route is closed:
#   .rollup(percentile, 95, 3600)  -> 400 "Unrecognized rollup method: percentile"
#   .rollup(p95, 3600)             -> 400 "Unrecognized rollup method: p95"
#   p95:<gauge>{...}               -> 200 but ZERO series (needs distribution metrics)
#   formula "p95(a)"               -> 400 'function "p95()" does not exist'
#   formula "percentile(a,95,3600)"-> 400 (percentile() is a SPACE aggregator;
#                                    args 2 and 3 are group tags, not q/window)
#   scalar aggregator "percentile" -> 400 "cannot use the sum aggregator with
#                                    the percentile reducer"
# .rollup() accepts only avg/sum/min/max/count, and both right-sizing metrics
# (kubernetes.cpu.usage.total, kubernetes.memory.usage) are GAUGES, so the
# distribution-only p95: prefix can never apply. Percentiles are therefore
# computed here, in Python, from the raw rolled-up points.
# ---------------------------------------------------------------------------

# Datadog caps a timeseries at 1500 points. Whole-minute granularities, smallest
# first; we pick the first that keeps a series under _TARGET_POINTS.
_INTERVAL_LADDER_MS = (60_000, 300_000, 600_000, 900_000, 1_800_000, 3_600_000, 7_200_000, 14_400_000)
_MIN_INTERVAL_MS = _INTERVAL_LADDER_MS[0]
_MAX_INTERVAL_MS = _INTERVAL_LADDER_MS[-1]
_TARGET_POINTS = 1000
# Datadog's hard per-series limit. A window long enough that even the 4h ceiling
# cannot fit under it (beyond ~250 days) gets a partial series back; say so
# rather than letting a partial window read as a complete one.
_DATADOG_POINT_CAP = 1500

# Budget against cap_tool_output's PASS_THROUGH_CHARS (40_000), NOT
# MAX_OUTPUT_SIZE (120_000). Anything above 40K is routed through an LLM
# summarizer, which would paraphrase percentiles into plausible fiction --
# strictly worse than truncation, because the numbers come back looking
# authoritative. ~280 B/row x 100 rows ~= 28 KB.
_MAX_STATS_SERIES = 100
_STATS_BYTE_BUDGET = 30_000

_SIG_DIGITS = 6


def _clamp_interval(interval: Optional[int]) -> Optional[int]:
    """Clamp a caller-supplied interval (ms) into the legal range, or None."""
    if interval is None:
        return None
    try:
        value = int(interval)
    except (TypeError, ValueError):
        return None
    return min(max(value, _MIN_INTERVAL_MS), _MAX_INTERVAL_MS)


def _pick_interval(span_ms: int, caller_interval: Optional[int] = None) -> int:
    """Choose a rollup granularity in ms that keeps a series under ~1000 points.

    A caller value wins (clamped). Otherwise walk the whole-minute ladder and
    take the finest granularity that stays under the point target -- a 30-day
    window lands on 3_600_000 (1h) for 720 points.
    """
    clamped = _clamp_interval(caller_interval)
    if clamped is not None:
        return clamped
    span_ms = max(int(span_ms), 0)
    for candidate in _INTERVAL_LADDER_MS:
        if span_ms // candidate <= _TARGET_POINTS:
            return candidate
    return _MAX_INTERVAL_MS


def _percentile(sorted_vals: list[float], q: float) -> Optional[float]:
    """Nearest-rank percentile: returns a real observed sample.

    Deliberately not statistics.quantiles -- these numbers land in a PR body as
    evidence a reviewer will look up in Datadog, so they must be a value that
    actually occurred. quantiles() interpolates, raises on n < 2, and its
    inclusive/exclusive methods disagree.
    """
    if not sorted_vals:
        # The max(0, ...) guard cannot rescue an empty list: min(-1, 0) is -1,
        # so the index expression would raise IndexError. Callers filter empty
        # series out first; this is defence in depth.
        return None
    idx = min(len(sorted_vals) - 1, max(0, math.ceil(q * len(sorted_vals)) - 1))
    return sorted_vals[idx]


def _round_sig(value: Optional[float], digits: int = _SIG_DIGITS) -> Optional[float]:
    """Round to N significant digits: keeps rows compact and keeps 10+ digit
    integers out of anything the agent might paste into a PR."""
    if value is None:
        return None
    if not math.isfinite(value) or value == 0:
        return 0.0 if value == 0 else None
    return round(value, -int(math.floor(math.log10(abs(value)))) + (digits - 1))


def _series_unit(unit: Any) -> Optional[str]:
    """Extract a unit name from Datadog's unit field (a 2-list of nullable dicts)."""
    if isinstance(unit, str):
        return unit
    if isinstance(unit, dict):
        return unit.get("name")
    if isinstance(unit, list):
        for entry in unit:
            if isinstance(entry, dict) and entry.get("name"):
                return entry["name"]
    return None


def _scope_and_group(group_tags: Any) -> tuple[str, dict]:
    """Build a stable scope string plus a k/v dict from a series' group_tags.

    scope is a sorted 'k:v' join so the agent can use it as a dict key across
    calls. Bare values (no colon) still appear in scope but cannot be keyed.
    """
    tags = [str(t) for t in group_tags if t] if isinstance(group_tags, list) else []
    group: dict[str, str] = {}
    for tag in tags:
        key, sep, value = tag.partition(":")
        if sep:
            group[key] = value
    return ",".join(sorted(tags)), group


def _summarize_series(group_tags: Any, unit: Any, row: Any) -> dict:
    """Reduce one series' points to a compact stats row (never per-point arrays)."""
    points = row if isinstance(row, list) else []
    # isfinite is not paranoia: a single NaN point breaks list.sort() (NaN
    # compares False against everything, so the "sorted" list stays unsorted and
    # vals[-1] is no longer the max), and json.dumps emits bare NaN/Infinity,
    # which is invalid JSON that the agent's parser rejects. bool is an int
    # subclass, so it is excluded explicitly.
    vals = [
        float(v) for v in points
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
    ]
    scope, group = _scope_and_group(group_tags)

    stats = {
        "scope": scope,
        "group": group,
        "unit": _series_unit(unit),
        "points": len(points),
        "nulls": len(points) - len(vals),
    }
    if not vals:
        # All-null or empty: report explicitly so the agent can tell "no data"
        # from "low usage" instead of recommending a cut on an idle-looking gap.
        stats.update({"p50": None, "p95": None, "p99": None, "max": None, "mean": None,
                      "note": "no non-null points in window"})
        return stats

    vals.sort()
    stats.update({
        "p50": _round_sig(_percentile(vals, 0.50)),
        "p95": _round_sig(_percentile(vals, 0.95)),
        "p99": _round_sig(_percentile(vals, 0.99)),
        "max": _round_sig(vals[-1]),
        "mean": _round_sig(sum(vals) / len(vals)),
    })
    return stats


def _p95_datadog(client, query: str, time_from: str, time_to: str, limit: int,
                 interval: Optional[int] = None) -> dict:
    """Datadog backend: fetch raw rolled-up points and reduce them in Python."""
    start_ms = _to_unix_ms(time_from)
    end_ms = _to_unix_ms(time_to)
    chosen_interval = _pick_interval(end_ms - start_ms, interval)

    response = client.query_metrics(
        query=query, start_ms=start_ms, end_ms=end_ms, interval=chosen_interval
    )
    data = response.get("data", {})
    attrs = data.get("attributes", {}) if isinstance(data, dict) else {}
    series = attrs.get("series") or []
    values = attrs.get("values") or []

    max_series = min(max(limit, 1), _MAX_STATS_SERIES)
    rows, used_bytes, truncated = [], 0, False
    for idx, entry in enumerate(series[:max_series]):
        meta = entry if isinstance(entry, dict) else {}
        row = _summarize_series(
            meta.get("group_tags"),
            meta.get("unit"),
            values[idx] if idx < len(values) else [],
        )
        row_bytes = len(json.dumps(row))
        if used_bytes + row_bytes > _STATS_BYTE_BUDGET:
            truncated = True
            break
        rows.append(row)
        used_bytes += row_bytes

    result = {
        "resource_type": "metric_stats",
        "metrics_source": "datadog",
        "interval_ms": chosen_interval,
        "series_returned": len(series),
        "series_included": len(rows),
        "count": len(rows),
        "results": rows,
    }

    expected_points = max(end_ms - start_ms, 0) // chosen_interval
    if expected_points > _DATADOG_POINT_CAP:
        # Even the coarsest legal interval cannot cover this window, so Datadog
        # returned a partial series. A percentile over a silently-clipped window
        # is not the percentile the caller asked for.
        result["window_exceeds_point_cap"] = True
        result["window_note"] = (
            f"This window needs ~{expected_points} points at the coarsest supported "
            f"interval, above Datadog's {_DATADOG_POINT_CAP}-point per-series limit, so the "
            "series is partial and these percentiles cover only part of the window. "
            "Use a window of 250 days or less."
        )

    if truncated:
        result["series_truncated"] = True
        result["note"] = (
            f"Only {len(rows)} of {len(series)} series fit the output budget. "
            "Narrow the query filter or group by fewer tags and re-run."
        )
    elif len(series) > max_series:
        result["series_dropped"] = len(series) - max_series
        result["note"] = (
            f"Returned {max_series} of {len(series)} series (per-call series cap). "
            "Narrow the query filter to see the rest."
        )
    return result


# Only one backend is implemented today. Adding New Relic, Dynatrace or Coroot
# is a table entry plus a branch that pushes the percentile into the query
# (percentile(x,95) / :percentile(95) / quantile_over_time) and reads back a
# scalar -- the row shape must stay identical so neither the prompt nor the
# Slack card ever learns which provider answered.
_METRIC_STATS_SOURCES = ("datadog",)
_P95_BY_SOURCE = {"datadog": _p95_datadog}

# Fail fast at import if a source is advertised without a dispatch entry --
# mirrors the _ALERTS_PATH_BY_SOURCE guard in aurora_mcp/tools_gated.py.
assert set(_P95_BY_SOURCE) >= set(_METRIC_STATS_SOURCES), (
    "metric_stats sources not fully covered by _P95_BY_SOURCE: "
    f"missing {set(_METRIC_STATS_SOURCES) - set(_P95_BY_SOURCE)}"
)


def _query_metric_stats(client, query: str, time_from: str, time_to: str, limit: int, **kwargs) -> dict:
    """Percentile summary per series -- one compact row per series, no raw points.

    One row PER SERIES is load-bearing, not cosmetic. _truncate_results keeps a
    *prefix* of `results`, so the single fat dict that resource_type='metrics'
    returns gets dropped whole: a 563 KB payload becomes count=0, which the
    agent cannot distinguish from "this workload has no data" -- and it would
    then recommend cutting an idle-looking workload. With N compact rows no
    single item can exceed the limit, so that failure mode cannot occur.
    """
    if not query:
        raise ValueError(
            "query is required for resource_type='metric_stats' "
            "(e.g., 'sum:kubernetes.memory.usage{env:production} by {kube_deployment}')"
        )
    source = (kwargs.get("source") or "datadog").lower().strip()
    backend = _P95_BY_SOURCE.get(source)
    if backend is None:
        raise ValueError(
            f"Unsupported metrics source '{source}' for metric_stats. "
            f"Supported: {', '.join(sorted(_P95_BY_SOURCE))}"
        )
    return backend(client, query, time_from, time_to, limit, interval=kwargs.get("interval"))


def _query_monitors(client, query: str, time_from: str, time_to: str, limit: int, **kwargs) -> dict:
    params: dict[str, Any] = {"page_size": limit}
    if query:
        params["name"] = query
    monitors = client.list_monitors(params=params)
    if not isinstance(monitors, list):
        monitors = []
    return {"resource_type": "monitors", "count": len(monitors[:limit]), "results": monitors[:limit]}


def _query_events(client, query: str, time_from: str, time_to: str, limit: int, **kwargs) -> dict:
    start_ts = _to_unix_seconds(time_from)
    end_ts = _to_unix_seconds(time_to)
    params: dict[str, Any] = {}
    if query:
        params["sources"] = query
    response = client.list_events(start_ts=start_ts, end_ts=end_ts, params=params)
    events = response.get("events", [])[:limit]
    return {"resource_type": "events", "count": len(events), "results": events}


def _query_traces(client, query: str, time_from: str, time_to: str, limit: int, **kwargs) -> dict:
    start = _to_iso8601(time_from)
    end = _to_iso8601(time_to)
    response = client.search_traces(query=query or "*", start=start, end=end, limit=limit)
    spans = response.get("data", [])[:limit]
    return {"resource_type": "traces", "count": len(spans), "results": spans}


def _query_hosts(client, query: str, time_from: str, time_to: str, limit: int, **kwargs) -> dict:
    from_ts = _to_unix_seconds(time_from)
    response = client.list_hosts(query=query, count=limit, from_ts=from_ts)
    host_list = response.get("host_list", [])[:limit]
    return {"resource_type": "hosts", "count": len(host_list), "results": host_list}


def _query_incidents(client, query: str, time_from: str, time_to: str, limit: int, **kwargs) -> dict:
    response = client.list_incidents(page_size=limit)
    incidents = response.get("data", [])[:limit]
    return {"resource_type": "incidents", "count": len(incidents), "results": incidents}


_HANDLERS = {
    "logs": _query_logs,
    "metrics": _query_metrics,
    "metric_stats": _query_metric_stats,
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
    interval: Optional[int] = None,
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    """Query Datadog for logs, metrics, metric_stats, monitors, events, traces, hosts, or incidents."""
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
        result = handler(client, query, time_from, time_to, limit, interval=interval)
        result["success"] = True
        result["time_range"] = f"{time_from} to {time_to}"

        results_list = result.get("results", [])
        serialized = [json.dumps(item) for item in results_list]
        truncated_results, was_truncated = _truncate_results(results_list, serialized)
        if was_truncated:
            result["results"] = truncated_results
            result["truncated"] = True
            # Append rather than assign: metric_stats already puts its own
            # series_dropped / series_truncated explanation in `note`, and
            # overwriting it would hide the fact that series were dropped too.
            _prior_note = result.get("note")
            _note = f"Results truncated from {len(results_list)} to {len(truncated_results)} due to size limit."
            result["note"] = f"{_prior_note} {_note}" if _prior_note else _note
            result["count"] = len(truncated_results)

        if was_truncated and not truncated_results and results_list:
            # A single oversized item leaves results empty, which reads as
            # count=0 -- indistinguishable from "no data" and a silent wrong
            # answer. Say so loudly instead.
            result["truncated_all"] = True
            result["note"] = ("A single result exceeded the output size limit; nothing could be "
                              "returned. Narrow the window or grouping, or use "
                              "resource_type='metric_stats' for percentile summaries.")

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
