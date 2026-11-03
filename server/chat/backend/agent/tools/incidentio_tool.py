"""incident.io investigation tools for RCA agent.

Provides four tools for RCA:
- list_incidentio_incidents: Find recent/related incidents for pattern analysis
- get_incidentio_incident: Deep-dive a specific incident (status, roles, custom fields, timestamps)
- get_incidentio_timeline: Fetch timeline updates for an incident to understand sequence of events
- get_incidentio_alert: Look up an alert by incident.io alert id or source deduplication key
"""

import json
import logging
import re
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from utils.auth.token_management import get_token_data

_ERR_NO_USER = json.dumps({"error": "No user context available"})
_ERR_NOT_CONNECTED = json.dumps({"error": "incident.io not connected"})
_ERR_INVALID_ID = json.dumps({"error": "Invalid incident_id"})
_ERR_INVALID_ALERT_ID = json.dumps({"error": "Invalid alert_id"})
_ERR_ALERT_NOT_FOUND = json.dumps({"error": "No incident.io alert found for that id"})

# incident.io alert ids are ULIDs. Anything else is a source-system key.
_INCIDENTIO_ALERT_ID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$", re.IGNORECASE)
_NOT_FOUND = "Resource not found"
# A non-ULID on /alerts/{id} is rejected (HTTP 400). A missing ULID is 404.
# Both should continue on to the deduplication-key search.
_PATH_MISS = frozenset({_NOT_FOUND, "incident.io API error (HTTP 400)"})

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


class GetAlertArgs(BaseModel):
    """Arguments for get_incidentio_alert."""
    alert_id: str = Field(
        description=(
            "An incident.io alert id, or the source system's own alert id. "
            "Source ids (Site24x7 and others) are stored as the alert's deduplication key."
        ),
    )


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
        # Alert ids 404 here. The alert tool also accepts source dedup keys.
        if result.get("error") == _NOT_FOUND:
            result = {
                "error": (
                    "Resource not found. If this is an alert id or a source-system "
                    "alert id (for example Site24x7), use get_incidentio_alert."
                ),
            }
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


def _attribute_value(raw: Any) -> str:
    """Human-readable label from an alert attribute value (object or multi-select)."""
    if isinstance(raw, dict):
        return str(raw.get("label") or raw.get("literal") or "")
    if isinstance(raw, list):
        labels = [
            str(item.get("label") or item.get("literal") or "")
            for item in raw[:5]
            if isinstance(item, dict)
        ]
        return ", ".join(label for label in labels if label)
    return ""


def _format_alert(alert: Dict[str, Any]) -> Dict[str, Any]:
    """Distill an alert into the fields useful for RCA."""
    attributes = []
    for attr in (alert.get("attributes") or [])[:20]:
        if not isinstance(attr, dict):
            continue
        name = (attr.get("attribute") or {}).get("name")
        if not name:
            continue
        attributes.append({"name": name, "value": _attribute_value(attr.get("value"))})

    description = alert.get("description") or ""
    if isinstance(description, str) and len(description) > 4000:
        description = description[:4000] + "... [truncated]"

    return {
        "id": alert.get("id"),
        "title": alert.get("title"),
        "description": description,
        "status": alert.get("status"),
        "deduplication_key": alert.get("deduplication_key"),
        "source_url": alert.get("source_url"),
        "alert_source_id": alert.get("alert_source_id"),
        "created_at": alert.get("created_at"),
        "updated_at": alert.get("updated_at"),
        "resolved_at": alert.get("resolved_at"),
        "alert_group_ids": alert.get("alert_group_ids") or [],
        "attributes": attributes,
    }


def _attached_incidents(api_key: str, alert_id: str) -> Dict[str, Any]:
    """Incidents this alert is attached to. A lookup failure is reported, not fatal."""
    result = _api_request(
        api_key, "GET", "/incident_alerts",
        params={"alert_id": alert_id, "page_size": 25},
    )
    if "error" in result:
        return {"incidents_error": result["error"]}

    incidents = []
    for link in (result.get("incident_alerts") or [])[:25]:
        if not isinstance(link, dict):
            continue
        incident = link.get("incident") or {}
        if not incident.get("id"):
            continue
        status = incident.get("incident_status")
        incidents.append({
            "id": incident.get("id"),
            "name": incident.get("name"),
            "reference": incident.get("reference"),
            "status": status.get("name") if isinstance(status, dict) else incident.get("status"),
            "status_category": incident.get("status_category"),
        })
    return {"incidents": incidents}


def _alert_from_show(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The alert object from GET /alerts/{id}, or None when the body isn't one."""
    alert = result.get("alert")
    if isinstance(alert, dict) and alert.get("id"):
        return alert
    # Some responses are the alert itself rather than {"alert": ...}.
    if result.get("id") and "alerts" not in result:
        return result
    return None


def _show_alert(api_key: str, alert_id: str) -> tuple:
    """(alert, error_json). A miss is (None, None) so the caller can try the dedup key."""
    # Source ids are not ULIDs. Putting one on the path 400s and hides the search.
    if not _INCIDENTIO_ALERT_ID.fullmatch(alert_id):
        return None, None
    direct = _api_request(api_key, "GET", f"/alerts/{alert_id}")
    # Hit on the incident.io alert id itself.
    if "error" not in direct:
        return _alert_from_show(direct), None
    # 404/400: not this alert. Auth and transport errors are final.
    if direct.get("error") in _PATH_MISS:
        return None, None
    return None, json.dumps(direct)


def _alerts_for_dedup_key(api_key: str, alert_id: str) -> tuple:
    """(matches, error_json, has_more) for a source-system deduplication key."""
    listed = _api_request(
        api_key, "GET", "/alerts",
        params={"page_size": 10, "deduplication_key[is]": alert_id},
    )
    if "error" in listed:
        return [], json.dumps(listed), False
    matches = [item for item in (listed.get("alerts") or []) if isinstance(item, dict)]
    # incident.io sends `after` on the last page too, so a short page is the end.
    has_more = len(matches) >= 10 and bool((listed.get("pagination_meta") or {}).get("after"))
    return matches, None, has_more


def _annotate_alerts(api_key: str, alerts: list) -> list:
    """Formatted alerts, each with the incidents it is attached to."""
    formatted = []
    for alert in alerts:
        item = _format_alert(alert)
        # Attachment lookup needs the incident.io id from the alert payload.
        if item.get("id"):
            item.update(_attached_incidents(api_key, item["id"]))
        formatted.append(item)
    return formatted


def get_incidentio_alert(
    alert_id: str,
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    """Look up an incident.io alert by its id or by a source-system deduplication key."""
    if not user_id:
        return _ERR_NO_USER

    api_key = _get_incidentio_credentials(user_id)
    if not api_key:
        return _ERR_NOT_CONNECTED

    alert_id = (alert_id or "").strip()
    # Control characters are never part of an id and would break the request.
    if not alert_id or len(alert_id) > 100 or any(ord(char) < 32 for char in alert_id):
        return _ERR_INVALID_ALERT_ID

    alert, error = _show_alert(api_key, alert_id)
    if error:
        return error

    # Not an incident.io alert id — source ids live on the deduplication key.
    if alert is None:
        matches, error, has_more = _alerts_for_dedup_key(api_key, alert_id)
        if error:
            return error
        # The source key is not a deduplication key we have an alert for.
        if not matches:
            return _ERR_ALERT_NOT_FOUND
        # The same source key can exist on more than one alert source.
        if len(matches) > 1:
            formatted = _annotate_alerts(api_key, matches)
            return _truncate_output({
                "matches": formatted,
                "total_returned": len(formatted),
                "matched_on": "deduplication_key",
                "has_more": has_more,
            })
        alert = matches[0]

    return _truncate_output(_annotate_alerts(api_key, [alert])[0])


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
