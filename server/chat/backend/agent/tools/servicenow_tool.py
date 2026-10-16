"""ServiceNow investigation tools for the RCA agent (read-only).

- list_servicenow_incidents: find recent or related incidents (state, priority,
  assignment group, free text)
- get_servicenow_incident: one incident by number (INC0010001) or sys_id
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from routes.servicenow.servicenow_client import ServiceNowAPIError, ServiceNowClient
from utils.auth.token_management import get_token_data

logger = logging.getLogger(__name__)

_ERR_NO_USER = json.dumps({"error": "No user context available"})
_ERR_NOT_CONNECTED = json.dumps({"error": "ServiceNow not connected"})
MAX_OUTPUT_SIZE = 500_000

# Encoded-query separators and control characters are stripped from free text
# so a tool argument can never add its own query clauses.
_QUERY_UNSAFE_RE = re.compile(r"[\^\r\n\t\x00-\x1f]")
_STATE_QUERIES = {
    "active": "active=true",
    "new": "state=1",
    "in_progress": "state=2",
    "on_hold": "state=3",
    "resolved": "state=6",
    "closed": "state=7",
    "canceled": "state=8",
}
_OUTPUT_FIELDS = (
    "number", "sys_id", "short_description", "description", "priority", "impact",
    "urgency", "state", "category", "subcategory", "assignment_group", "assigned_to",
    "cmdb_ci", "caller_id", "opened_at", "resolved_at", "closed_at", "close_notes",
    "sys_updated_on",
)


class ListServiceNowIncidentsArgs(BaseModel):
    """Arguments for list_servicenow_incidents."""
    state: Optional[str] = Field(
        default=None,
        description="Filter by state: 'active', 'new', 'in_progress', 'on_hold', 'resolved', 'closed', 'canceled', or a numeric state value. Leave empty for all.",
    )
    priority: Optional[int] = Field(
        default=None,
        description="Filter by priority 1 (critical) to 5 (planning).",
    )
    assignment_group: Optional[str] = Field(
        default=None,
        description="Filter by assignment group name (partial match).",
    )
    query: Optional[str] = Field(
        default=None,
        description="Free-text match on short description and description (partial match).",
    )
    limit: int = Field(default=25, description="Number of incidents to return (max 100).")


class GetServiceNowIncidentArgs(BaseModel):
    """Arguments for get_servicenow_incident."""
    incident: str = Field(description="Incident number (for example INC0010001) or sys_id.")


def _get_servicenow_credentials(user_id: str) -> Optional[Dict[str, Any]]:
    try:
        creds = get_token_data(user_id, "servicenow")
        return creds if creds and creds.get("instance_url") else None
    except Exception as exc:
        logger.error("[SERVICENOW-TOOL] Failed to get credentials: %s", exc)
        return None


def is_servicenow_connected(user_id: str) -> bool:
    return _get_servicenow_credentials(user_id) is not None


def _clean(text: str, max_len: int = 200) -> str:
    return _QUERY_UNSAFE_RE.sub(" ", str(text)).strip()[:max_len]


def build_query(
    state: Optional[str] = None,
    priority: Optional[int] = None,
    assignment_group: Optional[str] = None,
    query: Optional[str] = None,
) -> str:
    """Build a ServiceNow encoded query from the tool arguments."""
    parts: List[str] = []
    if state:
        key = _clean(state, 20).lower().replace("-", "_").replace(" ", "_")
        if key in _STATE_QUERIES:
            parts.append(_STATE_QUERIES[key])
        elif key.isdigit():
            parts.append(f"state={int(key)}")
    if priority is not None and 1 <= int(priority) <= 5:
        parts.append(f"priority={int(priority)}")
    if assignment_group:
        parts.append(f"assignment_group.nameLIKE{_clean(assignment_group)}")
    if query:
        text = _clean(query)
        if text:
            parts.append(f"short_descriptionLIKE{text}^ORdescriptionLIKE{text}")
    parts.append("ORDERBYDESCopened_at")
    return "^".join(parts)


def _format_incident(record: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for field in _OUTPUT_FIELDS:
        value = record.get(field)
        if value in (None, ""):
            continue
        if isinstance(value, dict):  # reference link form, when display values are off
            value = value.get("display_value") or value.get("value")
        if field == "description" and isinstance(value, str):
            value = value[:2000]
        out[field] = value
    return out


def _truncate_output(data: Any) -> str:
    text = json.dumps(data, ensure_ascii=False, default=str)
    if len(text) > MAX_OUTPUT_SIZE:
        return text[:MAX_OUTPUT_SIZE] + "\n...[truncated]"
    return text


def list_servicenow_incidents(
    state: Optional[str] = None,
    priority: Optional[int] = None,
    assignment_group: Optional[str] = None,
    query: Optional[str] = None,
    limit: int = 25,
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    """List ServiceNow incidents, newest first."""
    if not user_id:
        return _ERR_NO_USER
    creds = _get_servicenow_credentials(user_id)
    if not creds:
        return _ERR_NOT_CONNECTED

    encoded = build_query(state, priority, assignment_group, query)
    try:
        rows = ServiceNowClient.from_credentials(creds).list_incidents(query=encoded, limit=limit)
    except (ServiceNowAPIError, ValueError) as exc:
        return json.dumps({"error": str(exc)})

    incidents = [_format_incident(row) for row in rows]
    return _truncate_output({
        "incidents": incidents,
        "total_returned": len(incidents),
        "query": encoded,
    })


def get_servicenow_incident(
    incident: str,
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    """Get one ServiceNow incident by number or sys_id."""
    if not user_id:
        return _ERR_NO_USER
    creds = _get_servicenow_credentials(user_id)
    if not creds:
        return _ERR_NOT_CONNECTED

    try:
        record = ServiceNowClient.from_credentials(creds).get_incident(_clean(incident, 64))
    except (ServiceNowAPIError, ValueError) as exc:
        return json.dumps({"error": str(exc)})
    if not record:
        return json.dumps({"error": f"Incident {_clean(incident, 64)} not found"})
    return _truncate_output({"incident": _format_incident(record)})
