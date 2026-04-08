"""Unified OpsGenie query tool for the RCA agent."""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from pydantic import BaseModel, Field

from routes.opsgenie.config import MAX_OUTPUT_SIZE, MAX_RESULTS_CAP
from routes.opsgenie.opsgenie_routes import (
    _get_stored_opsgenie_credentials,
    _build_client_from_creds,
    OpsGenieAPIError,
)

logger = logging.getLogger(__name__)


class QueryOpsGenieArgs(BaseModel):
    resource_type: str = Field(
        description="Type of OpsGenie resource to query: 'alerts', 'alert_details', "
        "'incidents', 'incident_details', 'services', 'on_call', 'schedules', 'teams'"
    )
    query: str = Field(
        default="",
        description="OpsGenie query syntax (e.g. 'status=open AND priority=P1')",
    )
    identifier: str = Field(
        default="",
        description="Alert/incident/schedule ID for detail queries",
    )
    time_from: str = Field(
        default="-1h",
        description="Start time (relative like '-1h', '-24h' or ISO 8601)",
    )
    time_to: str = Field(
        default="now",
        description="End time",
    )
    limit: int = Field(default=50, description="Maximum number of results to return")


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------


def is_opsgenie_connected(user_id: str) -> bool:
    """Check if a user has valid OpsGenie credentials stored."""
    try:
        creds = _get_stored_opsgenie_credentials(user_id)
        if not creds:
            return False
        client = _build_client_from_creds(creds)
        return client is not None
    except Exception:
        logger.debug("OpsGenie connection check failed for user %s", user_id)
        return False


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


def _query_alerts(client, query: str, limit: int) -> dict:
    response = client.list_alerts(query=query or None, limit=limit)
    data = response.get("data", [])[:limit]
    return {"resource_type": "alerts", "count": len(data), "results": data}


def _query_alert_details(client, identifier: str) -> dict:
    with ThreadPoolExecutor(max_workers=3) as pool:
        f_alert = pool.submit(client.get_alert, identifier)
        f_logs = pool.submit(client.get_alert_logs, identifier)
        f_notes = pool.submit(client.get_alert_notes, identifier)
    return {
        "resource_type": "alert_details",
        "results": [{"alert": f_alert.result().get("data", {}),
                      "logs": f_logs.result().get("data", []),
                      "notes": f_notes.result().get("data", [])}],
        "count": 1,
    }


def _query_incidents(client, query: str, limit: int) -> dict:
    response = client.list_incidents(query=query or None, limit=limit)
    data = response.get("data", [])[:limit]
    result = {"resource_type": "incidents", "count": len(data), "results": data}
    if response.get("note"):
        result["note"] = response["note"]
    return result


def _query_incident_details(client, identifier: str) -> dict:
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_incident = pool.submit(client.get_incident, identifier)
        f_timeline = pool.submit(client.get_incident_timeline, identifier)
    return {
        "resource_type": "incident_details",
        "results": [{"incident": f_incident.result().get("data", {}),
                      "timeline": f_timeline.result().get("data", [])}],
        "count": 1,
    }


def _query_services(client, limit: int) -> dict:
    response = client.list_services(limit=limit)
    data = response.get("data", [])[:limit]
    return {"resource_type": "services", "count": len(data), "results": data}


def _query_on_call(client, identifier: str) -> dict:
    if identifier:
        response = client.get_on_calls(identifier)
        data = response.get("data", {})
        return {"resource_type": "on_call", "count": 1, "results": [data]}

    # No specific schedule — list all schedules and get on-call for each (parallel)
    schedules_resp = client.list_schedules()
    schedules = [s for s in schedules_resp.get("data", []) if s.get("id")]

    def _fetch_on_call(schedule):
        try:
            oc_resp = client.get_on_calls(schedule["id"])
            oc_data = oc_resp.get("data", {})
            oc_data["schedule_name"] = schedule.get("name", "")
            return oc_data
        except OpsGenieAPIError:
            logger.warning("[OPSGENIE-TOOL] Failed to get on-call for schedule %s", schedule["id"])
            return None

    if not schedules:
        return {"resource_type": "on_call", "count": 0, "results": []}
    with ThreadPoolExecutor(max_workers=min(len(schedules), 8)) as pool:
        on_call_results = [r for r in pool.map(_fetch_on_call, schedules) if r is not None]
    return {"resource_type": "on_call", "count": len(on_call_results), "results": on_call_results}


def _query_schedules(client) -> dict:
    response = client.list_schedules()
    data = response.get("data", [])
    return {"resource_type": "schedules", "count": len(data), "results": data}


def _query_teams(client) -> dict:
    response = client.list_teams()
    data = response.get("data", [])
    return {"resource_type": "teams", "count": len(data), "results": data}


_HANDLERS = {
    "alerts": lambda client, **kw: _query_alerts(client, kw.get("query", ""), kw.get("limit", 50)),
    "alert_details": lambda client, **kw: _query_alert_details(client, kw.get("identifier", "")),
    "incidents": lambda client, **kw: _query_incidents(client, kw.get("query", ""), kw.get("limit", 50)),
    "incident_details": lambda client, **kw: _query_incident_details(client, kw.get("identifier", "")),
    "services": lambda client, **kw: _query_services(client, kw.get("limit", 50)),
    "on_call": lambda client, **kw: _query_on_call(client, kw.get("identifier", "")),
    "schedules": lambda client, **kw: _query_schedules(client),
    "teams": lambda client, **kw: _query_teams(client),
}

_DETAIL_TYPES = {"alert_details", "incident_details"}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def query_opsgenie(
    resource_type: str,
    query: str = "",
    identifier: str = "",
    time_from: str = "-1h",
    time_to: str = "now",
    limit: int = 50,
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    """Query OpsGenie for alerts, incidents, services, on-call schedules, or teams."""
    if not user_id:
        return json.dumps({"error": "User context not available"})

    creds = _get_stored_opsgenie_credentials(user_id)
    if not creds:
        return json.dumps({"error": "OpsGenie not connected. Please connect OpsGenie first."})

    client = _build_client_from_creds(creds)
    if not client:
        return json.dumps({"error": "Failed to build OpsGenie client from stored credentials."})

    resource_type = resource_type.lower().strip()
    handler = _HANDLERS.get(resource_type)
    if not handler:
        return json.dumps({"error": f"Invalid resource_type '{resource_type}'. Must be one of: {', '.join(_HANDLERS)}"})

    if resource_type in _DETAIL_TYPES and not identifier:
        return json.dumps({"error": f"identifier is required for resource_type='{resource_type}'"})

    limit = min(max(limit, 1), MAX_RESULTS_CAP)
    logger.info("[OPSGENIE-TOOL] user=%s resource=%s query=%s", user_id, resource_type, query[:100] if query else "")

    try:
        result = handler(client, query=query, identifier=identifier, limit=limit)
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

    except OpsGenieAPIError as exc:
        if exc.status_code == 429:
            return json.dumps({"error": "OpsGenie API rate limit reached. Please retry later."})
        if exc.status_code in (401, 403):
            return json.dumps({"error": "OpsGenie authentication failed. API key may be invalid or expired."})
        logger.exception("[OPSGENIE-TOOL] API error for user=%s resource=%s", user_id, resource_type)
        return json.dumps({"error": f"OpsGenie API error: {str(exc)[:200]}"})
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    except Exception as exc:
        logger.exception("[OPSGENIE-TOOL] Query failed for user=%s resource=%s", user_id, resource_type)
        return json.dumps({"error": f"Error querying OpsGenie: {str(exc)}"})
