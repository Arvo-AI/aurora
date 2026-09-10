"""Elastic read routes: indices, fields, search, ES|QL, Kibana alerts and rules."""

import logging
import re
from typing import Any, Dict, List, Optional

from flask import Blueprint, jsonify, request

from connectors.elastic_connector.client import (
    MAX_SEARCH_SIZE,
    ElasticAPIError,
    ElasticClient,
    build_log_query,
    compact_hits,
    resolve_window,
)
from utils.auth.rbac_decorators import require_permission
from utils.auth.token_management import get_token_data
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

search_bp = Blueprint("elastic_search", __name__)

DEFAULT_INDEX_PATTERN = "logs-*"
MAX_INDICES_RETURN = 500
MAX_FIELDS_RETURN = 500
MAX_ESQL_ROWS = 500
_LIMIT_RE = re.compile(r"\|\s*limit\s+\d+", re.IGNORECASE)


def _arg(data: Dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in data and data[name] is not None:
            return data[name]
    return default


def _client_for_user(user_id: str):
    """Return (client, index_pattern) or (None, None)."""
    try:
        creds = get_token_data(user_id, "elastic")
    except Exception as exc:
        logger.error("[ELASTIC-SEARCH] Failed to get credentials for user %s: %s", sanitize(user_id), sanitize(exc))
        return None, None
    if not creds or not creds.get("api_key") or not creds.get("elasticsearch_url"):
        return None, None
    client = ElasticClient(creds["elasticsearch_url"], creds["api_key"], kibana_url=creds.get("kibana_url"))
    return client, creds.get("index_pattern") or DEFAULT_INDEX_PATTERN


def _error_response(exc: ElasticAPIError):
    if exc.status_code == 400:
        return jsonify({"error": str(exc)}), 400
    if exc.status_code in (401, 403):
        return jsonify({"error": str(exc)}), 400
    if exc.status_code == 404:
        return jsonify({"error": str(exc)}), 404
    return jsonify({"error": str(exc)}), 502


def _int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(int(value), hi))
    except (TypeError, ValueError):
        return default


_HEALTH_RANK = {"green": 0, "yellow": 1, "red": 2}


def _worst_health(values: List[Optional[str]]) -> Optional[str]:
    known = [v for v in values if v in _HEALTH_RANK]
    return max(known, key=_HEALTH_RANK.__getitem__) if known else None


def merge_index_listing(
    resolved: Dict[str, Any],
    cat: Optional[List[Dict[str, Any]]],
    include_system: bool = True,
    limit: int = MAX_INDICES_RETURN,
) -> Dict[str, Any]:
    """Merge ``_resolve/index`` with optional ``_cat/indices`` enrichment.

    ``_cat/indices`` only knows concrete indices, so a data stream's stats are
    aggregated over its backing indices. System (dot-prefixed) entries are
    dropped before the cap when ``include_system`` is false so ``total`` and
    ``truncated`` describe what the caller can actually see.
    """
    stats: Dict[str, Dict[str, Any]] = {}
    for row in cat or []:
        name = row.get("index")
        if name:
            stats[name] = row

    def _entry(
        name: str,
        kind: str,
        extra: Optional[Dict[str, Any]] = None,
        backing: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        item: Dict[str, Any] = {"name": name, "kind": kind}
        if extra:
            item.update(extra)
        rows = [stats[n] for n in (backing or [name]) if n in stats]
        if rows:
            docs = [_to_int(r.get("docs.count")) for r in rows]
            sizes = [_to_int(r.get("store.size")) for r in rows]
            item["docs"] = sum(d for d in docs if d is not None)
            item["sizeBytes"] = sum(s for s in sizes if s is not None)
            item["health"] = _worst_health([r.get("health") for r in rows])
        return item

    entries: List[Dict[str, Any]] = []
    for ds in resolved.get("data_streams") or []:
        backing = ds.get("backing_indices") or []
        entries.append(_entry(ds.get("name", ""), "data_stream", {
            "timestampField": ds.get("timestamp_field"),
            "backingIndices": len(backing),
        }, backing=backing))
    for alias in resolved.get("aliases") or []:
        entries.append(_entry(alias.get("name", ""), "alias", {"indices": (alias.get("indices") or [])[:20]}))
    for idx in resolved.get("indices") or []:
        name = idx.get("name", "")
        # Backing indices of data streams are noisy; keep them but flag them.
        entries.append(_entry(name, "index", {
            "dataStream": idx.get("data_stream"),
            "aliases": (idx.get("aliases") or [])[:10],
        }))

    if not include_system:
        entries = _hide_system(entries)
    total = len(entries)
    entries = entries[:limit]
    return {
        "indices": entries,
        "count": len(entries),
        "total": total,
        "truncated": total > len(entries),
        "statsAvailable": cat is not None,
        "note": None if cat is not None else (
            "Document counts and sizes omitted: the API key lacks the cluster 'monitor' privilege "
            "required by _cat/indices."
        ),
    }


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _hide_system(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [e for e in entries if not e["name"].startswith(".") or e["name"].startswith(".alerts-")]


@search_bp.route("/indices", methods=["GET"])
@require_permission("connectors", "read")
def list_indices(user_id):
    """List indices, aliases and data streams matching ``pattern`` (default ``*``)."""
    client, _ = _client_for_user(user_id)
    if not client:
        return jsonify({"error": "Elastic not connected"}), 400
    pattern = request.args.get("pattern") or "*"
    include_system = request.args.get("includeSystem", request.args.get("include_system", "false")).lower() == "true"
    try:
        resolved = client.resolve_indices(pattern)
        cat = client.cat_indices(pattern)
    except ElasticAPIError as exc:
        logger.warning("[ELASTIC-SEARCH] list_indices failed for user %s: %s", sanitize(user_id), sanitize(exc))
        return _error_response(exc)
    listing = merge_index_listing(resolved, cat, include_system=include_system)
    listing["pattern"] = pattern
    return jsonify(listing)


@search_bp.route("/fields", methods=["GET"])
@require_permission("connectors", "read")
def list_fields(user_id):
    """Field names/types for an index pattern (``_field_caps``)."""
    client, default_index = _client_for_user(user_id)
    if not client:
        return jsonify({"error": "Elastic not connected"}), 400
    index = request.args.get("index") or default_index
    prefix = request.args.get("prefix") or ""
    try:
        caps = client.field_caps(index, f"{prefix}*" if prefix else "*")
    except ElasticAPIError as exc:
        logger.warning("[ELASTIC-SEARCH] list_fields failed for user %s: %s", sanitize(user_id), sanitize(exc))
        return _error_response(exc)
    fields = summarize_field_caps(caps, prefix)
    return jsonify({
        "index": index,
        "fields": fields[:MAX_FIELDS_RETURN],
        "count": min(len(fields), MAX_FIELDS_RETURN),
        "total": len(fields),
        "truncated": len(fields) > MAX_FIELDS_RETURN,
    })


def summarize_field_caps(caps: Dict[str, Any], prefix: str = "") -> List[Dict[str, Any]]:
    fields: List[Dict[str, Any]] = []
    for name, types in (caps.get("fields") or {}).items():
        if name.startswith("_") or (prefix and not name.startswith(prefix)):
            continue
        type_names = sorted(types.keys()) if isinstance(types, dict) else []
        if type_names == ["object"] or type_names == ["nested"]:
            continue
        searchable = any(bool(t.get("searchable")) for t in types.values()) if isinstance(types, dict) else False
        aggregatable = any(bool(t.get("aggregatable")) for t in types.values()) if isinstance(types, dict) else False
        fields.append({
            "name": name,
            "type": "|".join(type_names),
            "searchable": searchable,
            "aggregatable": aggregatable,
        })
    fields.sort(key=lambda f: f["name"])
    return fields


@search_bp.route("/search", methods=["POST"])
@require_permission("connectors", "read")
def search(user_id):
    """Query DSL / Lucene search over logs.

    Body (camelCase or snake_case): ``index``, ``query`` (Lucene query_string),
    ``queryDsl`` (raw Query DSL object, used instead of ``query``),
    ``timeRangeMinutes`` or ``timeRange`` ("15m"/"2h") or ``startTime``/``endTime``,
    ``limit`` (≤500), ``fields`` (list of dotted names), ``timestampField``.
    """
    client, default_index = _client_for_user(user_id)
    if not client:
        return jsonify({"error": "Elastic not connected"}), 400
    data = request.get_json(silent=True) or {}

    index = _arg(data, "index") or default_index
    query = _arg(data, "query", "q", default="*")
    query_dsl = _arg(data, "queryDsl", "query_dsl")
    limit = _int(_arg(data, "limit", "size", "maxCount", "max_count", default=100), 100, 1, MAX_SEARCH_SIZE)
    fields = _arg(data, "fields")
    if fields is not None and not isinstance(fields, list):
        return jsonify({"error": "fields must be a list of field names"}), 400
    timestamp_field = _arg(data, "timestampField", "timestamp_field", default="@timestamp")
    if not isinstance(timestamp_field, str) or not re.match(r"^[A-Za-z0-9_.@-]{1,128}$", timestamp_field):
        return jsonify({"error": "timestampField is invalid"}), 400
    sort_order = "asc" if str(_arg(data, "sort", "order", default="desc")).lower() == "asc" else "desc"

    start, end = resolve_window(
        time_range=_arg(data, "timeRange", "time_range"),
        start_time=_arg(data, "startTime", "start_time"),
        end_time=_arg(data, "endTime", "end_time"),
        minutes=_arg(data, "timeRangeMinutes", "time_range_minutes"),
    )

    if query_dsl is not None and not isinstance(query_dsl, dict):
        return jsonify({"error": "queryDsl must be a Query DSL object"}), 400
    if query_dsl:
        query_body = {"bool": {"must": [query_dsl], "filter": [{"range": {timestamp_field: {"gte": start, "lte": end}}}]}}
    else:
        query_body = build_log_query(str(query or "*"), start, end, timestamp_field=timestamp_field)

    body: Dict[str, Any] = {
        "size": limit,
        "sort": [{timestamp_field: {"order": sort_order, "unmapped_type": "date"}}],
        "query": query_body,
        "track_total_hits": 10000,
    }
    if fields:
        body["_source"] = [str(f) for f in fields][:100]

    logger.info("[ELASTIC-SEARCH] User %s search index=%s query=%s", sanitize(user_id), sanitize(index), sanitize(str(query))[:100])
    try:
        result = client.search(index, body)
    except ElasticAPIError as exc:
        logger.warning("[ELASTIC-SEARCH] search failed for user %s: %s", sanitize(user_id), sanitize(exc))
        return _error_response(exc)

    hits_obj = result.get("hits") or {}
    total = hits_obj.get("total")
    if isinstance(total, dict):
        total_value = total.get("value")
        total_relation = total.get("relation", "eq")
    else:
        total_value, total_relation = total, "eq"
    hits = compact_hits(hits_obj.get("hits") or [], fields=[str(f) for f in fields] if fields else None)
    return jsonify({
        "success": True,
        "index": index,
        "query": query if not query_dsl else None,
        "queryDsl": query_dsl,
        "timeRange": {"start": start, "end": end},
        "total": total_value,
        "totalRelation": total_relation,
        "count": len(hits),
        "took": result.get("took"),
        "timedOut": result.get("timed_out"),
        "hits": hits,
    })


@search_bp.route("/esql", methods=["POST"])
@require_permission("connectors", "read")
def esql(user_id):
    """Run an ES|QL query (``POST /_query``) with a time pre-filter."""
    client, _ = _client_for_user(user_id)
    if not client:
        return jsonify({"error": "Elastic not connected"}), 400
    data = request.get_json(silent=True) or {}
    query = _arg(data, "query", "esql")
    if not query or not isinstance(query, str):
        return jsonify({"error": "query is required"}), 400
    query = query.strip()
    if len(query) > 10000:
        return jsonify({"error": "query is too long"}), 400
    timestamp_field = _arg(data, "timestampField", "timestamp_field", default="@timestamp")
    if not isinstance(timestamp_field, str) or not re.match(r"^[A-Za-z0-9_.@-]{1,128}$", timestamp_field):
        return jsonify({"error": "timestampField is invalid"}), 400

    minutes = _arg(data, "timeRangeMinutes", "time_range_minutes")
    time_range = _arg(data, "timeRange", "time_range")
    filter_dsl: Optional[Dict[str, Any]] = None
    if minutes is not None or time_range:
        start, end = resolve_window(time_range=time_range, minutes=minutes)
        filter_dsl = {"range": {timestamp_field: {"gte": start, "lte": end}}}
    else:
        start = end = None

    if not _LIMIT_RE.search(query):
        row_limit = _int(_arg(data, "limit"), 100, 1, MAX_ESQL_ROWS)
        query = f"{query} | LIMIT {row_limit}"

    logger.info("[ELASTIC-SEARCH] User %s ES|QL: %s", sanitize(user_id), sanitize(query)[:120])
    try:
        result = client.esql(query, filter_dsl)
    except ElasticAPIError as exc:
        logger.warning("[ELASTIC-SEARCH] esql failed for user %s: %s", sanitize(user_id), sanitize(exc))
        return _error_response(exc)

    columns = result.get("columns") or []
    values = result.get("values") or []
    truncated = len(values) > MAX_ESQL_ROWS
    return jsonify({
        "success": True,
        "query": query,
        "timeRange": {"start": start, "end": end} if start else None,
        "columns": columns,
        "values": values[:MAX_ESQL_ROWS],
        "rowCount": min(len(values), MAX_ESQL_ROWS),
        "truncated": truncated,
        "took": result.get("took"),
    })


@search_bp.route("/alerts/active", methods=["GET"])
@require_permission("connectors", "read")
def active_alerts(user_id):
    """Kibana alert documents from ``.alerts-*`` (status active|recovered|all)."""
    client, _ = _client_for_user(user_id)
    if not client:
        return jsonify({"error": "Elastic not connected"}), 400
    status = (request.args.get("status") or "active").lower()
    if status not in ("active", "recovered", "all"):
        return jsonify({"error": "status must be active, recovered or all"}), 400
    hours = _int(request.args.get("hours"), 24, 1, 24 * 90)
    limit = _int(request.args.get("limit"), 50, 1, MAX_SEARCH_SIZE)
    rule_name = request.args.get("ruleName") or request.args.get("rule_name")
    try:
        result = client.search_alerts(status=status, hours=hours, size=limit, rule_name=rule_name)
    except ElasticAPIError as exc:
        logger.warning("[ELASTIC-SEARCH] active_alerts failed for user %s: %s", sanitize(user_id), sanitize(exc))
        return _error_response(exc)
    alerts = format_alert_hits(result)
    total = (result.get("hits") or {}).get("total")
    return jsonify({
        "status": status,
        "hours": hours,
        "alerts": alerts,
        "count": len(alerts),
        "total": total.get("value") if isinstance(total, dict) else total,
    })


def format_alert_hits(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    alerts: List[Dict[str, Any]] = []
    for hit in (result.get("hits") or {}).get("hits") or []:
        src = hit.get("_source") or {}
        alerts.append({
            "index": hit.get("_index"),
            "timestamp": src.get("@timestamp"),
            "uuid": _nested(src, "kibana.alert.uuid"),
            "status": _nested(src, "kibana.alert.status"),
            "start": _nested(src, "kibana.alert.start"),
            "end": _nested(src, "kibana.alert.end"),
            "reason": _nested(src, "kibana.alert.reason"),
            "ruleName": _nested(src, "kibana.alert.rule.name"),
            "ruleUuid": _nested(src, "kibana.alert.rule.uuid"),
            "ruleCategory": _nested(src, "kibana.alert.rule.category"),
            "ruleTags": _nested(src, "kibana.alert.rule.tags"),
            "instanceId": _nested(src, "kibana.alert.instance.id"),
            "value": _nested(src, "kibana.alert.evaluation.value"),
            "threshold": _nested(src, "kibana.alert.evaluation.threshold"),
            "severity": _nested(src, "kibana.alert.severity"),
            "workflowStatus": _nested(src, "kibana.alert.workflow_status"),
            "spaceIds": _nested(src, "kibana.space_ids"),
            "serviceName": _nested(src, "service.name"),
            "hostName": _nested(src, "host.name"),
        })
    return alerts


def _nested(obj: Dict[str, Any], dotted: str) -> Any:
    """Read ``a.b.c`` from either a nested dict or a flat dotted key."""
    if dotted in obj:
        return obj[dotted]
    current: Any = obj
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


@search_bp.route("/rules", methods=["GET"])
@require_permission("connectors", "read")
def list_rules(user_id):
    """Kibana alerting rules (``/api/alerting/rules/_find``)."""
    client, _ = _client_for_user(user_id)
    if not client:
        return jsonify({"error": "Elastic not connected"}), 400
    if not client.kibana_url:
        return jsonify({"error": "No Kibana URL configured for this connection. Reconnect with a Kibana URL or Cloud ID."}), 400
    search_term = request.args.get("search")
    per_page = _int(request.args.get("perPage") or request.args.get("per_page"), 50, 1, 100)
    page = _int(request.args.get("page"), 1, 1, 1000)
    space = request.args.get("space")
    try:
        result = client.find_rules(page=page, per_page=per_page, search=search_term, space=space)
    except ElasticAPIError as exc:
        logger.warning("[ELASTIC-SEARCH] list_rules failed for user %s: %s", sanitize(user_id), sanitize(exc))
        return _error_response(exc)
    rules = [
        {
            "id": r.get("id"),
            "name": r.get("name"),
            "ruleTypeId": r.get("rule_type_id"),
            "consumer": r.get("consumer"),
            "enabled": r.get("enabled"),
            "tags": r.get("tags"),
            "schedule": (r.get("schedule") or {}).get("interval"),
            "executionStatus": (r.get("execution_status") or {}).get("status"),
            "lastExecutionDate": (r.get("execution_status") or {}).get("last_execution_date"),
            "actionCount": len(r.get("actions") or []),
        }
        for r in result.get("data") or []
    ]
    return jsonify({
        "rules": rules,
        "count": len(rules),
        "total": result.get("total"),
        "page": result.get("page", page),
        "perPage": result.get("per_page", per_page),
    })
