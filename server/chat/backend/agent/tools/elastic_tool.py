"""Elastic Cloud (Elasticsearch + Kibana) read-only tools for the RCA agent.

Same contract as ``splunk_tool``: functions take ``user_id`` (injected by
``with_user_context``), return JSON strings and never raise.
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from connectors.elastic_connector.client import (
    TIMESTAMP_FIELD,
    ElasticAPIError,
    ElasticClient,
    build_log_query,
    compact_hits,
    contains_script_clause,
    resolve_window,
)
from utils.auth.token_management import get_token_data
from utils.flags.feature_flags import is_elastic_enabled
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

MAX_OUTPUT_SIZE = 2 * 1024 * 1024  # 2MB max output
MAX_INDICES_RETURN = 300
MAX_FIELDS_RETURN = 400
MAX_SEARCH_HITS = 500
MAX_ESQL_ROWS = 500
DEFAULT_INDEX_PATTERN = "logs-*"
_LIMIT_RE = re.compile(r"\|\s*limit\s+\d+", re.IGNORECASE)
_TS_FIELD_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,128}$")


# --------------------------------------------------------------------------- #
# Arg schemas
# --------------------------------------------------------------------------- #


class ElasticListIndicesArgs(BaseModel):
    """Arguments for elastic_list_indices."""
    pattern: str = Field(default="*", description="Index/data-stream/alias pattern, e.g. 'logs-*', 'filebeat-*', '*'")


class ElasticGetFieldsArgs(BaseModel):
    """Arguments for elastic_get_fields."""
    index: str = Field(description="Index pattern to inspect, e.g. 'logs-*' or 'logs-nginx.access-default'")
    prefix: Optional[str] = Field(default=None, description="Only return fields starting with this prefix, e.g. 'kubernetes.' or 'http.'")


class ElasticSearchLogsArgs(BaseModel):
    """Arguments for elastic_search_logs."""
    query: str = Field(default="*", description="Lucene query_string, e.g. 'log.level:error AND service.name:\"checkout\"'. Use '*' for everything.")
    index: Optional[str] = Field(default=None, description="Index pattern (defaults to the connection's configured pattern, usually 'logs-*')")
    time_range: str = Field(default="1h", description="Relative window ending now: '15m', '1h', '24h', '7d'. Ignored if start_time/end_time given.")
    start_time: Optional[str] = Field(default=None, description="ISO-8601 start, e.g. '2026-01-05T10:00:00Z'")
    end_time: Optional[str] = Field(default=None, description="ISO-8601 end (defaults to now)")
    limit: int = Field(default=100, description="Max documents (≤500). Newest first.")
    fields: Optional[List[str]] = Field(default=None, description="Only return these dotted fields, e.g. ['@timestamp','message','host.name']")
    query_dsl: Optional[Dict[str, Any]] = Field(default=None, description="Raw Elasticsearch Query DSL object used instead of `query` (advanced)")
    timestamp_field: str = Field(default=TIMESTAMP_FIELD, description="Date field used for the time window and sort. Change it for indices that do not use @timestamp (e.g. 'timestamp', 'event_time'); check with elastic_get_fields.")


class ElasticEsqlArgs(BaseModel):
    """Arguments for elastic_esql."""
    query: str = Field(description="ES|QL query, e.g. 'FROM logs-* | WHERE log.level == \"error\" | STATS count() BY service.name | SORT `count()` DESC'")
    time_range: Optional[str] = Field(default="1h", description="Relative time pre-filter on timestamp_field: '15m', '1h', '24h', '7d'. Pass 'all' to disable it (required for indices without a date field, e.g. lookup/config indices).")
    timestamp_field: str = Field(default=TIMESTAMP_FIELD, description="Timestamp field for the time pre-filter")


class ElasticGetAlertsArgs(BaseModel):
    """Arguments for elastic_get_alerts."""
    status: str = Field(default="active", description="'active', 'recovered' or 'all'")
    hours: int = Field(default=24, description="Look-back window in hours")
    limit: int = Field(default=50, description="Max alerts (≤500)")
    rule_name: Optional[str] = Field(default=None, description="Filter by Kibana rule name (query_string syntax, e.g. 'cpu*')")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _get_elastic_credentials(user_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not user_id or not is_elastic_enabled():
        return None
    try:
        creds = get_token_data(user_id, "elastic")
        if not creds:
            return None
        if not creds.get("api_key") or not creds.get("elasticsearch_url"):
            logger.warning("[ELASTIC-TOOL] Credentials exist but missing api_key or elasticsearch_url")
            return None
        return creds
    except Exception:
        logger.exception("[ELASTIC-TOOL] Failed to get credentials")
        return None


def is_elastic_connected(user_id: str) -> bool:
    """True when the feature flag is on and the user has a stored Elastic connection."""
    return _get_elastic_credentials(user_id) is not None


def _client(creds: Dict[str, Any]) -> ElasticClient:
    return ElasticClient(creds["elasticsearch_url"], creds["api_key"], kibana_url=creds.get("kibana_url"))


def _not_connected() -> str:
    return json.dumps({"error": "Elastic not connected. Connect Elastic Cloud in Aurora first."})


def _error(exc: Exception, what: str) -> str:
    if isinstance(exc, ElasticAPIError):
        return json.dumps({"error": f"{what} failed: {exc}", "status_code": exc.status_code})
    logger.error("[ELASTIC-TOOL] %s failed: %s", what, exc)
    return json.dumps({"error": f"{what} failed: {type(exc).__name__}"})


def _fit_output(items: List[Any], max_size: int = MAX_OUTPUT_SIZE) -> tuple:
    """Trim a list so its JSON stays under ``max_size``. Returns (items, was_truncated)."""
    kept: List[Any] = []
    total = 0
    for item in items:
        size = len(json.dumps(item, default=str))
        if total + size > max_size:
            return kept, True
        kept.append(item)
        total += size
    return kept, False


def _int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(int(value), hi))
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #


def elastic_list_indices(pattern: str = "*", user_id: Optional[str] = None, **kwargs) -> str:
    """List indices, data streams and aliases matching a pattern."""
    creds = _get_elastic_credentials(user_id)
    if not creds:
        return _not_connected()
    from routes.elastic.search_routes import merge_index_listing

    pattern = (pattern or "*").strip() or "*"
    logger.info("[ELASTIC-TOOL] list_indices user=%s pattern=%s", sanitize(user_id), pattern[:80])
    try:
        client = _client(creds)
        resolved = client.resolve_indices(pattern)
        cat = client.cat_indices(pattern)
    except Exception as exc:
        return _error(exc, "List indices")

    # Filter + cap once, inside the shared helper, so `total` is the true
    # post-filter count rather than a count of an already-capped list.
    listing = merge_index_listing(resolved, cat, include_system=False, limit=MAX_INDICES_RETURN)
    entries = listing["indices"]
    total = listing["total"]
    result: Dict[str, Any] = {
        "success": True,
        "pattern": pattern,
        "default_index_pattern": creds.get("index_pattern") or DEFAULT_INDEX_PATTERN,
        "index_count": len(entries),
        "total": total,
        "indices": entries,
    }
    if listing.get("truncated"):
        result["truncated"] = True
        result["note"] = f"Showing {len(entries)} of {total}. Use a narrower pattern."
    if listing.get("note"):
        result["stats_note"] = listing["note"]
    return json.dumps(result, default=str)


def elastic_get_fields(index: str, prefix: Optional[str] = None, user_id: Optional[str] = None, **kwargs) -> str:
    """List field names and types for an index pattern."""
    creds = _get_elastic_credentials(user_id)
    if not creds:
        return _not_connected()
    from routes.elastic.search_routes import summarize_field_caps

    requested_index = (index or "").strip()
    index = requested_index or creds.get("index_pattern") or DEFAULT_INDEX_PATTERN
    prefix = (prefix or "").strip()
    logger.info("[ELASTIC-TOOL] get_fields user=%s index=%s prefix=%s", sanitize(user_id), requested_index[:80] or "<default>", prefix[:40])
    try:
        caps = _client(creds).field_caps(index, f"{prefix}*" if prefix else "*")
    except Exception as exc:
        return _error(exc, "Get fields")

    fields = summarize_field_caps(caps, prefix)
    result: Dict[str, Any] = {
        "success": True,
        "index": index,
        "field_count": min(len(fields), MAX_FIELDS_RETURN),
        "total": len(fields),
        "fields": [{"name": f["name"], "type": f["type"]} for f in fields[:MAX_FIELDS_RETURN]],
    }
    if len(fields) > MAX_FIELDS_RETURN:
        result["truncated"] = True
        result["note"] = "Use `prefix` to narrow the field list."
    return json.dumps(result, default=str)


def elastic_search_logs(
    query: str = "*",
    index: Optional[str] = None,
    time_range: str = "1h",
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    limit: int = 100,
    fields: Optional[List[str]] = None,
    query_dsl: Optional[Dict[str, Any]] = None,
    timestamp_field: str = TIMESTAMP_FIELD,
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    """Search log documents with a Lucene query_string (or raw Query DSL)."""
    creds = _get_elastic_credentials(user_id)
    if not creds:
        return _not_connected()

    requested_index = (index or "").strip()
    index = requested_index or creds.get("index_pattern") or DEFAULT_INDEX_PATTERN
    limit = _int(limit, 100, 1, MAX_SEARCH_HITS)
    timestamp_field = (timestamp_field or TIMESTAMP_FIELD).strip()
    if not _TS_FIELD_RE.match(timestamp_field):
        return json.dumps({"error": "timestamp_field is invalid"})
    start, end = resolve_window(time_range=time_range, start_time=start_time, end_time=end_time)
    if query_dsl is not None and not isinstance(query_dsl, dict):
        return json.dumps({"error": "query_dsl must be a Query DSL object"})
    if query_dsl and contains_script_clause(query_dsl):
        return json.dumps({"error": "query_dsl must not contain script clauses"})
    if fields is not None and not isinstance(fields, list):
        return json.dumps({"error": "fields must be a list of field names"})

    if query_dsl:
        query_body: Dict[str, Any] = {
            "bool": {"must": [query_dsl], "filter": [{"range": {timestamp_field: {"gte": start, "lte": end}}}]}
        }
    else:
        query_body = build_log_query(query or "*", start, end, timestamp_field=timestamp_field)

    body: Dict[str, Any] = {
        "size": limit,
        "sort": [{timestamp_field: {"order": "desc", "unmapped_type": "date"}}],
        "query": query_body,
        "track_total_hits": 10000,
    }
    if fields:
        body["_source"] = [str(f) for f in fields][:100]

    logger.info("[ELASTIC-TOOL] search user=%s index=%s query=%s", sanitize(user_id), requested_index[:80] or "<default>", (query or "")[:100])
    try:
        result = _client(creds).search(index, body)
    except Exception as exc:
        return _error(exc, "Search")

    hits_obj = result.get("hits") or {}
    total = hits_obj.get("total")
    total_value = total.get("value") if isinstance(total, dict) else total
    total_relation = total.get("relation", "eq") if isinstance(total, dict) else "eq"
    compacted = compact_hits(hits_obj.get("hits") or [], fields=[str(f) for f in fields] if fields else None)
    original_count = len(compacted)
    compacted, was_truncated = _fit_output(compacted)

    response: Dict[str, Any] = {
        "success": True,
        "index": index,
        "query": query if not query_dsl else "(query_dsl)",
        "time_range": {"start": start, "end": end, "field": timestamp_field},
        "total": total_value,
        "total_relation": total_relation,
        "result_count": len(compacted),
        "results": compacted,
    }
    if total_value and total_value > len(compacted):
        response["note"] = (
            f"Showing {len(compacted)} of {total_value}{'+' if total_relation == 'gte' else ''} matching documents "
            "(newest first). Narrow the query, time range, or use elastic_esql with STATS to aggregate."
        )
    if was_truncated:
        response["truncated"] = True
        response["note"] = (
            f"Results truncated from {original_count} to {len(compacted)} due to size limit. "
            "Use `fields` to select specific fields or a smaller `limit`."
        )
    return json.dumps(response, default=str)


def elastic_esql(
    query: str,
    time_range: Optional[str] = "1h",
    timestamp_field: str = TIMESTAMP_FIELD,
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    """Run an ES|QL query with an optional relative time pre-filter (``time_range='all'`` disables it)."""
    creds = _get_elastic_credentials(user_id)
    if not creds:
        return _not_connected()
    if not query or not isinstance(query, str):
        return json.dumps({"error": "query is required"})
    query = query.strip()
    if len(query) > 10000:
        return json.dumps({"error": "query is too long"})
    if not timestamp_field or not _TS_FIELD_RE.match(timestamp_field):
        return json.dumps({"error": "timestamp_field is invalid"})
    if not _LIMIT_RE.search(query):
        query = f"{query} | LIMIT 100"

    # A range filter on a field the index lacks matches nothing, so the filter
    # must be optional for lookup/config indices without a date field.
    no_filter = time_range is None or str(time_range).strip().lower() in ("", "all", "none")
    if no_filter:
        start = end = None
        filter_dsl: Optional[Dict[str, Any]] = None
    else:
        start, end = resolve_window(time_range=time_range)
        filter_dsl = {"range": {timestamp_field: {"gte": start, "lte": end}}}
    logger.info("[ELASTIC-TOOL] esql user=%s query=%s", sanitize(user_id), query[:120])
    try:
        result = _client(creds).esql(query, filter_dsl)
    except Exception as exc:
        return _error(exc, "ES|QL query")

    columns = [{"name": c.get("name"), "type": c.get("type")} for c in result.get("columns") or []]
    values = result.get("values") or []
    original = len(values)
    values = values[:MAX_ESQL_ROWS]
    values, was_truncated = _fit_output(values)
    response: Dict[str, Any] = {
        "success": True,
        "query": query,
        "time_range": {"start": start, "end": end, "field": timestamp_field} if start else None,
        "columns": columns,
        "row_count": len(values),
        "rows": values,
    }
    if was_truncated or original > len(values):
        response["truncated"] = True
        response["note"] = f"Rows truncated from {original} to {len(values)}. Add a smaller `| LIMIT n` or aggregate with STATS."
    return json.dumps(response, default=str)


def elastic_get_alerts(
    status: str = "active",
    hours: int = 24,
    limit: int = 50,
    rule_name: Optional[str] = None,
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    """List Kibana alert documents from the ``.alerts-*`` indices."""
    creds = _get_elastic_credentials(user_id)
    if not creds:
        return _not_connected()
    from routes.elastic.search_routes import format_alert_hits

    status = (status or "active").lower()
    if status not in ("active", "recovered", "all"):
        return json.dumps({"error": "status must be 'active', 'recovered' or 'all'"})
    hours = _int(hours, 24, 1, 24 * 90)
    limit = _int(limit, 50, 1, MAX_SEARCH_HITS)
    logger.info("[ELASTIC-TOOL] get_alerts user=%s status=%s hours=%s", sanitize(user_id), status, hours)
    try:
        result = _client(creds).search_alerts(status=status, hours=hours, size=limit, rule_name=rule_name)
    except Exception as exc:
        return _error(exc, "Get alerts")

    alerts = format_alert_hits(result)
    alerts, was_truncated = _fit_output(alerts)
    total = (result.get("hits") or {}).get("total")
    response: Dict[str, Any] = {
        "success": True,
        "status": status,
        "hours": hours,
        "alert_count": len(alerts),
        "total": total.get("value") if isinstance(total, dict) else total,
        "alerts": alerts,
    }
    if was_truncated:
        response["truncated"] = True
    return json.dumps(response, default=str)
