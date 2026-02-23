"""Unified Dynatrace query tool for the RCA agent."""

import json
import logging
from typing import Any, Dict, Optional

import requests
from pydantic import BaseModel, Field

from utils.auth.token_management import get_token_data

logger = logging.getLogger(__name__)

DYNATRACE_TIMEOUT = 60
MAX_OUTPUT_SIZE = 2 * 1024 * 1024
MAX_RESULTS_CAP = 500


class QueryDynatraceArgs(BaseModel):
    resource_type: str = Field(description="Type of data to query: 'problems', 'logs', 'metrics', or 'entities'")
    query: str = Field(default="", description="Query/selector string. For problems: problem selector e.g. status(\"open\"). For logs: search query. For metrics: metric selector e.g. builtin:host.cpu.usage. For entities: entity selector e.g. type(\"HOST\")")
    time_from: str = Field(default="now-2h", description="Start of time range (e.g., 'now-1h', 'now-24h')")
    time_to: str = Field(default="now", description="End of time range")
    limit: int = Field(default=100, description="Maximum results to return")


def _get_credentials(user_id: str) -> Optional[Dict[str, Any]]:
    try:
        creds = get_token_data(user_id, "dynatrace")
        if creds and creds.get("api_token") and creds.get("environment_url"):
            return creds
    except Exception as exc:
        logger.error("[DYNATRACE-TOOL] Failed to get credentials: %s", exc)
    return None


def _headers(api_token: str) -> Dict[str, str]:
    return {"Authorization": f"Api-Token {api_token}", "Accept": "application/json"}


def _truncate_results(results: list) -> tuple[list, bool]:
    truncated, total_size = [], 0
    for item in results:
        item_str = json.dumps(item)
        if total_size + len(item_str) > MAX_OUTPUT_SIZE:
            return truncated, True
        truncated.append(item)
        total_size += len(item_str)
    return truncated, False


def is_dynatrace_connected(user_id: str) -> bool:
    return _get_credentials(user_id) is not None


def _query_problems(creds: Dict, query: str, time_from: str, time_to: str, limit: int) -> Dict:
    params: Dict[str, Any] = {"from": time_from, "to": time_to, "pageSize": min(limit, 500)}
    if query:
        params["problemSelector"] = query

    resp = requests.get(
        f"{creds['environment_url']}/api/v2/problems",
        headers=_headers(creds["api_token"]), params=params, timeout=DYNATRACE_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    problems = data.get("problems", [])[:limit]

    return {
        "resource_type": "problems",
        "count": len(problems),
        "total": data.get("totalCount", len(problems)),
        "results": [
            {
                "problemId": p.get("problemId"),
                "displayId": p.get("displayId"),
                "title": p.get("title"),
                "status": p.get("status"),
                "severity": p.get("severityLevel"),
                "impact": p.get("impactLevel"),
                "startTime": p.get("startTime"),
                "endTime": p.get("endTime"),
                "impactedEntities": [e.get("name") for e in p.get("impactedEntities", [])],
                "rootCauseEntity": p.get("rootCauseEntity", {}).get("name"),
                "managementZones": [z.get("name") for z in p.get("managementZones", [])],
            }
            for p in problems
        ],
    }


def _query_logs(creds: Dict, query: str, time_from: str, time_to: str, limit: int) -> Dict:
    params: Dict[str, Any] = {"from": time_from, "to": time_to, "limit": min(limit, 1000)}
    if query:
        params["query"] = query

    resp = requests.get(
        f"{creds['environment_url']}/api/v2/logs/search",
        headers=_headers(creds["api_token"]), params=params, timeout=DYNATRACE_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    results = data.get("results", [])[:limit]

    return {"resource_type": "logs", "count": len(results), "results": results}


def _query_metrics(creds: Dict, query: str, time_from: str, time_to: str, limit: int) -> Dict:
    if not query:
        return {"error": "metric selector is required for resource_type='metrics' (e.g., 'builtin:host.cpu.usage')"}

    params: Dict[str, Any] = {"metricSelector": query, "from": time_from, "to": time_to, "resolution": "1h"}
    resp = requests.get(
        f"{creds['environment_url']}/api/v2/metrics/query",
        headers=_headers(creds["api_token"]), params=params, timeout=DYNATRACE_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()

    series = []
    for result in data.get("result", []):
        for d in result.get("data", [])[:limit]:
            series.append({
                "metric": result.get("metricId"),
                "dimensions": d.get("dimensionMap"),
                "timestamps": d.get("timestamps"),
                "values": d.get("values"),
            })

    return {"resource_type": "metrics", "count": len(series), "results": series}


def _query_entities(creds: Dict, query: str, time_from: str, time_to: str, limit: int) -> Dict:
    params: Dict[str, Any] = {"from": time_from, "to": time_to, "pageSize": min(limit, 500)}
    # entitySelector is required by Dynatrace API - use a broad selector if none provided
    params["entitySelector"] = query if query else "type(HOST),type(SERVICE),type(APPLICATION),type(PROCESS_GROUP)"

    resp = requests.get(
        f"{creds['environment_url']}/api/v2/entities",
        headers=_headers(creds["api_token"]), params=params, timeout=DYNATRACE_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    entities = data.get("entities", [])[:limit]

    return {
        "resource_type": "entities",
        "count": len(entities),
        "total": data.get("totalCount", len(entities)),
        "results": [
            {
                "entityId": e.get("entityId"),
                "displayName": e.get("displayName"),
                "type": e.get("type"),
                "tags": [t.get("stringRepresentation") for t in e.get("tags", [])],
                "managementZones": [z.get("name") for z in e.get("managementZones", [])],
            }
            for e in entities
        ],
    }


_HANDLERS = {
    "problems": _query_problems,
    "logs": _query_logs,
    "metrics": _query_metrics,
    "entities": _query_entities,
}


def query_dynatrace(
    resource_type: str,
    query: str = "",
    time_from: str = "now-2h",
    time_to: str = "now",
    limit: int = 100,
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    """Query Dynatrace for problems, logs, metrics, or entities."""
    if not user_id:
        return json.dumps({"error": "User context not available"})

    creds = _get_credentials(user_id)
    if not creds:
        return json.dumps({"error": "Dynatrace not connected. Please connect Dynatrace first."})

    resource_type = resource_type.lower().strip()
    handler = _HANDLERS.get(resource_type)
    if not handler:
        return json.dumps({"error": f"Invalid resource_type '{resource_type}'. Must be one of: problems, logs, metrics, entities"})

    limit = min(max(limit, 1), MAX_RESULTS_CAP)
    logger.info("[DYNATRACE-TOOL] user=%s resource=%s query=%s", user_id, resource_type, query[:100] if query else "")

    try:
        result = handler(creds, query, time_from, time_to, limit)
        if "error" in result:
            return json.dumps(result)

        result["success"] = True
        result["time_range"] = f"{time_from} to {time_to}"

        results_list = result.get("results", [])
        truncated_results, was_truncated = _truncate_results(results_list)
        if was_truncated:
            result["results"] = truncated_results
            result["truncated"] = True
            result["note"] = f"Results truncated from {len(results_list)} to {len(truncated_results)} due to size limit."
            result["count"] = len(truncated_results)

        return json.dumps(result)

    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        if status == 401:
            return json.dumps({"error": "Dynatrace authentication failed. Token may be expired."})
        elif status == 403:
            scope_needed = {"logs": "logs.read", "metrics": "metrics.read", "events": "events.read"}.get(resource_type)
            if scope_needed:
                return json.dumps({"error": f"Token missing '{scope_needed}' scope, or this feature is not active in your Dynatrace environment."})
            return json.dumps({"error": "Token lacks required scope for this resource type."})
        elif status == 400:
            msg = exc.response.text[:200] if exc.response is not None else "Bad request"
            return json.dumps({"error": f"Invalid query: {msg}"})
        return json.dumps({"error": f"Dynatrace API error ({status})"})
    except requests.exceptions.Timeout:
        return json.dumps({"error": "Query timed out. Try a narrower time range."})
    except requests.exceptions.RequestException as exc:
        logger.error("[DYNATRACE-TOOL] Query failed: %s", exc)
        return json.dumps({"error": f"Query failed: {str(exc)}"})
