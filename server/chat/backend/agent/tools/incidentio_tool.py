"""incident.io investigation tools for RCA agent.

Provides three tools for RCA:
- list_incidentio_incidents: Find recent/related incidents for pattern analysis
- get_incidentio_incident: Deep-dive a specific incident (status, roles, custom fields, timestamps)
- get_incidentio_timeline: Fetch timeline updates for an incident to understand sequence of events
"""

import json
import logging
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from utils.auth.token_management import get_token_data

_ERR_NO_USER = json.dumps({"error": "No user context available"})
_ERR_NOT_CONNECTED = json.dumps({"error": "incident.io not connected"})
_ERR_INVALID_ID = json.dumps({"error": "Invalid incident_id"})

logger = logging.getLogger(__name__)

INCIDENTIO_API_BASE = "https://api.incident.io/v2"
INCIDENTIO_TIMEOUT = 20
MAX_OUTPUT_SIZE = 500_000


class ListIncidentsArgs(BaseModel):
    """Arguments for list_incidentio_incidents."""
    status: Optional[str] = Field(
        default=None,
        description="Filter by status category: 'live', 'closed', 'declined'. Leave empty for all.",
    )
    severity: Optional[str] = Field(
        default=None,
        description="Filter by severity ID or name (e.g., 'critical', 'major').",
    )
    page_size: int = Field(
        default=25,
        description="Number of incidents to return (max 100).",
    )
    after: Optional[str] = Field(
        default=None,
        description="Pagination cursor from a previous response's 'next_cursor'. Use to fetch the next page.",
    )


class GetIncidentArgs(BaseModel):
    """Arguments for get_incidentio_incident."""
    incident_id: str = Field(description="The incident.io incident ID to retrieve.")


class GetTimelineArgs(BaseModel):
    """Arguments for get_incidentio_timeline."""
    incident_id: str = Field(description="The incident.io incident ID to get timeline for.")


def _get_incidentio_credentials(user_id: str) -> Optional[str]:
    try:
        creds = get_token_data(user_id, "incidentio")
        if not creds:
            return None
        api_key = creds.get("api_key") or creds.get("token") or creds.get("access_token")
        return api_key if api_key else None
    except Exception as exc:
        logger.error("[INCIDENTIO-TOOL] Failed to get credentials: %s", exc)
        return None


def is_incidentio_connected(user_id: str) -> bool:
    return _get_incidentio_credentials(user_id) is not None


def _api_request(api_key: str, method: str, path: str, params: Optional[Dict] = None) -> Dict[str, Any]:
    import requests

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    url = f"{INCIDENTIO_API_BASE}{path}"
    try:
        resp = requests.request(method, url, headers=headers, params=params, timeout=INCIDENTIO_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.Timeout:
        return {"error": "Request to incident.io timed out"}
    except requests.exceptions.ConnectionError:
        return {"error": "Unable to reach incident.io API"}
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response else None
        if status == 401:
            return {"error": "Authentication failed — API key may be invalid or expired"}
        if status == 403:
            return {"error": "API key lacks required permissions"}
        if status == 404:
            return {"error": "Resource not found"}
        return {"error": f"incident.io API error (HTTP {status})"}


def _format_incident_summary(incident: Dict[str, Any]) -> Dict[str, Any]:
    """Distill an incident object into the fields useful for RCA."""
    severity = incident.get("severity") or {}
    inc_type = incident.get("incident_type") or {}
    roles = incident.get("incident_role_assignments") or []

    formatted_roles = []
    for r in roles[:10]:
        role_def = r.get("role", {})
        assignee = r.get("assignee", {})
        formatted_roles.append({
            "role": role_def.get("name", "unknown"),
            "assignee": assignee.get("name", "unassigned"),
        })

    custom_fields = []
    for cf in (incident.get("custom_field_entries") or [])[:20]:
        field_def = cf.get("custom_field", {})
        values = cf.get("values") or []
        val_labels = [v.get("label") or v.get("value_text") or str(v) for v in values[:3]]
        custom_fields.append({
            "field": field_def.get("name", "unknown"),
            "values": val_labels,
        })

    return {
        "id": incident.get("id"),
        "name": incident.get("name"),
        "status": incident.get("status"),
        "severity": severity.get("name") if isinstance(severity, dict) else str(severity),
        "type": inc_type.get("name") if isinstance(inc_type, dict) else str(inc_type),
        "summary": incident.get("summary") or "",
        "created_at": incident.get("created_at"),
        "updated_at": incident.get("updated_at"),
        "permalink": incident.get("permalink"),
        "roles": formatted_roles,
        "custom_fields": custom_fields,
        "duration_seconds": _calculate_duration(incident),
    }


def _calculate_duration(incident: Dict[str, Any]) -> Optional[int]:
    from datetime import datetime
    created = incident.get("created_at")
    closed = incident.get("closed_at")
    if not created:
        return None
    try:
        start = datetime.fromisoformat(created.replace("Z", "+00:00"))
        if closed:
            end = datetime.fromisoformat(closed.replace("Z", "+00:00"))
        else:
            end = datetime.now(start.tzinfo)
        return int((end - start).total_seconds())
    except Exception:
        return None


def _truncate_output(data: Any) -> str:
    output = json.dumps(data, default=str)
    if len(output) <= MAX_OUTPUT_SIZE:
        return output
    if isinstance(data, dict) and "incidents" in data and isinstance(data["incidents"], list):
        items = data["incidents"]
        while items and len(json.dumps(data, default=str)) > MAX_OUTPUT_SIZE:
            items.pop()
        data["truncated"] = True
        data["total_returned"] = len(items)
        return json.dumps(data, default=str)
    return output[:MAX_OUTPUT_SIZE]


def list_incidentio_incidents(
    status: Optional[str] = None,
    severity: Optional[str] = None,
    page_size: int = 25,
    after: Optional[str] = None,
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    """List incidents from incident.io. Use 'after' cursor to paginate through large result sets."""
    if not user_id:
        return _ERR_NO_USER

    api_key = _get_incidentio_credentials(user_id)
    if not api_key:
        return _ERR_NOT_CONNECTED

    page_size = min(max(page_size, 1), 100)
    params: Dict[str, Any] = {"page_size": page_size}
    if status:
        params["status_category[one_of]"] = status
    if severity:
        params["severity[one_of]"] = severity
    if after:
        params["after"] = after

    result = _api_request(api_key, "GET", "/incidents", params=params)
    if "error" in result:
        return json.dumps(result)

    incidents = result.get("incidents") or []
    summaries = [_format_incident_summary(inc) for inc in incidents]

    pagination = result.get("pagination_meta") or {}
    output: Dict[str, Any] = {
        "incidents": summaries,
        "total_returned": len(summaries),
    }
    if pagination.get("after"):
        output["next_cursor"] = pagination["after"]
        output["has_more"] = True
    else:
        output["has_more"] = False
    if "total_record_count" in pagination:
        output["total_count"] = pagination["total_record_count"]

    return _truncate_output(output)


def get_incidentio_incident(
    incident_id: str,
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    """Get full details of a specific incident.io incident for deep-dive investigation."""
    if not user_id:
        return _ERR_NO_USER

    api_key = _get_incidentio_credentials(user_id)
    if not api_key:
        return _ERR_NOT_CONNECTED

    if not incident_id or len(incident_id) > 100:
        return _ERR_INVALID_ID

    result = _api_request(api_key, "GET", f"/incidents/{incident_id}")
    if "error" in result:
        return json.dumps(result)

    incident = result.get("incident") or result
    summary = _format_incident_summary(incident)

    # Include additional detail not in the list view
    summary["timestamps"] = {
        "created_at": incident.get("created_at"),
        "updated_at": incident.get("updated_at"),
        "closed_at": incident.get("closed_at"),
        "last_activity_at": incident.get("last_activity_at"),
    }
    summary["workload_minutes"] = incident.get("workload_minutes_total")
    summary["slack_channel"] = (incident.get("slack_channel_name") or
                                 (incident.get("incident_channel") or {}).get("name"))

    return _truncate_output(summary)


def get_incidentio_timeline(
    incident_id: str,
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    """Get timeline/updates for an incident to understand the sequence of events and decisions."""
    if not user_id:
        return _ERR_NO_USER

    api_key = _get_incidentio_credentials(user_id)
    if not api_key:
        return _ERR_NOT_CONNECTED

    if not incident_id or len(incident_id) > 100:
        return _ERR_INVALID_ID

    result = _api_request(api_key, "GET", "/incident_updates", params={"incident_id": incident_id})
    if "error" in result:
        return json.dumps(result)

    updates = result.get("incident_updates") or []
    formatted = []
    for u in updates[:50]:
        formatted.append({
            "id": u.get("id"),
            "message": u.get("message") or u.get("new_value") or "",
            "created_at": u.get("created_at"),
            "updater": (u.get("updater") or {}).get("name", "system"),
            "new_status": u.get("new_incident_status", {}).get("name") if u.get("new_incident_status") else None,
            "new_severity": u.get("new_severity", {}).get("name") if u.get("new_severity") else None,
        })

    return _truncate_output({
        "incident_id": incident_id,
        "updates": formatted,
        "total_returned": len(formatted),
    })
