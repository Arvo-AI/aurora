"""ServiceNow tools for the RCA agent.

Read (always bound when ServiceNow is connected):
- list_servicenow_incidents: find recent or related incidents (state, priority,
  assignment group, free text)
- get_servicenow_incident: one incident by number (INC0010001) or sys_id

Write (bound only when the org turned on "Post work notes on ServiceNow incidents"):
- servicenow_add_work_note: a work note (or customer-visible comment) on an incident
- servicenow_create_incident / servicenow_update_incident: "full" write mode only
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from routes.servicenow.servicenow_client import (
    ServiceNowAPIError,
    ServiceNowClient,
    incident_url,
    is_sys_id,
)
from utils.auth.token_management import get_token_data

logger = logging.getLogger(__name__)

_ERR_NO_USER = json.dumps({"error": "No user context available"})
_ERR_NOT_CONNECTED = json.dumps({"error": "ServiceNow not connected"})
MAX_OUTPUT_SIZE = 500_000
_MAX_NOTE = 8000
_MAX_TEXT = 4000
_INCIDENT_REF = "Incident number (for example INC0010001) or sys_id."

# Encoded-query separators and control characters are stripped from free text
# so a tool argument can never add its own query clauses.
_QUERY_UNSAFE_RE = re.compile(r"[\^\x00-\x1f]")
_STATE_QUERIES = {
    "active": "active=true",
    "new": "state=1",
    "in_progress": "state=2",
    "on_hold": "state=3",
    "resolved": "state=6",
    "closed": "state=7",
    "canceled": "state=8",
}
# State names the write tools accept, mapped to the incident state values.
_STATE_VALUES = {
    "new": "1", "in_progress": "2", "on_hold": "3", "resolved": "6", "closed": "7",
    "canceled": "8", "cancelled": "8",
}
_OUTPUT_FIELDS = (
    "number", "sys_id", "short_description", "description", "priority", "impact",
    "urgency", "state", "category", "subcategory", "assignment_group", "assigned_to",
    "cmdb_ci", "caller_id", "opened_at", "resolved_at", "closed_at", "close_notes",
    "sys_updated_on",
)
# Fields the agent may change on an existing incident (a subset of the client's allowlist).
_UPDATABLE_FIELDS = (
    "short_description", "description", "priority", "impact", "urgency", "state",
    "category", "subcategory", "assignment_group", "assigned_to", "cmdb_ci",
    "close_code", "close_notes", "work_notes", "comments",
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
    incident: str = Field(description=_INCIDENT_REF)


class ServiceNowAddWorkNoteArgs(BaseModel):
    """Arguments for servicenow_add_work_note."""
    incident: str = Field(description=_INCIDENT_REF)
    note: str = Field(description="The note text (markdown is shown as plain text in ServiceNow).")
    customer_visible: bool = Field(
        default=False,
        description="False posts an internal work note. True posts an additional comment the caller can see.",
    )


class ServiceNowCreateIncidentArgs(BaseModel):
    """Arguments for servicenow_create_incident."""
    short_description: str = Field(description="One-line summary of the incident (required).")
    description: str = Field(default="", description="Longer description: symptoms, impact, what was found.")
    priority: Optional[int] = Field(default=None, description="Priority 1 (critical) to 5 (planning). Leave empty to let ServiceNow derive it from impact and urgency.")
    impact: Optional[int] = Field(default=None, description="Impact 1 (high) to 3 (low).")
    urgency: Optional[int] = Field(default=None, description="Urgency 1 (high) to 3 (low).")
    category: Optional[str] = Field(default=None, description="Incident category label as shown in ServiceNow, for example 'Software'.")
    assignment_group: Optional[str] = Field(default=None, description="Assignment group name.")
    caller: Optional[str] = Field(default=None, description="Caller display name. Leave empty for the connected account's default.")
    cmdb_ci: Optional[str] = Field(default=None, description="Configuration item name.")


class ServiceNowUpdateIncidentArgs(BaseModel):
    """Arguments for servicenow_update_incident."""
    incident: str = Field(description=_INCIDENT_REF)
    fields: Dict[str, Any] = Field(
        description=(
            "Fields to change. Allowed: short_description, description, priority (1-5), impact (1-3), "
            "urgency (1-3), state ('new', 'in_progress', 'on_hold', 'resolved', 'closed', 'canceled' or a value), "
            "category, subcategory, assignment_group, assigned_to, cmdb_ci, close_code, close_notes, "
            "work_notes, comments. Resolving requires close_code and close_notes on most instances."
        ),
    )


# ── gates ────────────────────────────────────────────────────────────


def _require_servicenow_write(user_id: str) -> None:
    """Every ServiceNow write needs the org's opt-in: chat, Actions and unattended RCAs alike.

    The "Post work notes" switch on the connector page is the single control over
    what Aurora writes to ServiceNow; the session type never bypasses it.
    """
    from routes.servicenow.tasks import work_notes_enabled

    if not work_notes_enabled(user_id):
        raise ValueError(
            "ServiceNow write-back is disabled. Turn on \"Post work notes on ServiceNow incidents\" "
            "under Connectors > ServiceNow before posting."
        )


def _require_servicenow_full_write_mode(user_id: str) -> None:
    from routes.servicenow.tasks import get_write_mode

    if get_write_mode(user_id) != "full":
        raise ValueError(
            "ServiceNow is set to work notes only. Choose \"Create & update incidents\" under "
            "Connectors > ServiceNow to create or update incidents."
        )


def _forget_write_access_on_403(user_id: str, exc: Exception) -> None:
    """ServiceNow answered 403: the account cannot write, so stop retrying it on every later call.

    Same downgrade the completion work note and the resolution sync apply.
    """
    if not (isinstance(exc, ServiceNowAPIError) and exc.status_code == 403):
        return
    creds = _get_servicenow_credentials(user_id)
    if creds:
        from utils.notifications.servicenow_notification_service import disable_write_capability

        disable_write_capability(user_id, creds)


# ── helpers ──────────────────────────────────────────────────────────


def _get_servicenow_credentials(user_id: str) -> Optional[Dict[str, Any]]:
    try:
        creds = get_token_data(user_id, "servicenow")
        return creds if creds and creds.get("instance_url") else None
    except Exception:
        logger.exception("[SERVICENOW-TOOL] Failed to get credentials")
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


def _resolve_sys_id(client: ServiceNowClient, ref: str) -> Optional[str]:
    """sys_id for a number or sys_id; None when the incident does not exist."""
    ref = _clean(ref, 64)
    if is_sys_id(ref):
        return ref
    record = client.get_incident(ref, fields="sys_id,number")
    sys_id = (record or {}).get("sys_id")
    return str(sys_id) if sys_id else None


def _choice(value: Any, low: int, high: int) -> Optional[str]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(str(value).strip()[:1]) if isinstance(value, str) and str(value).strip()[:1].isdigit() else int(value)
    except (TypeError, ValueError):
        return None
    return str(number) if low <= number <= high else None


def _prepare_update_fields(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise the agent's field map onto what the Table API expects."""
    out: Dict[str, Any] = {}
    for key, value in (fields or {}).items():
        if key not in _UPDATABLE_FIELDS or value in (None, ""):
            continue
        if key == "priority":
            value = _choice(value, 1, 5)
        elif key in ("impact", "urgency"):
            value = _choice(value, 1, 3)
        elif key == "state":
            text = str(value).strip().lower().replace("-", "_").replace(" ", "_")
            value = _STATE_VALUES.get(text) or (text if text in ("1", "2", "3", "6", "7", "8") else None)
        elif key in ("work_notes", "comments", "description", "close_notes"):
            value = str(value)[:_MAX_NOTE]
        else:
            value = str(value)[:_MAX_TEXT]
        if value is None:
            raise ValueError(f"Unsupported value for {key}")
        out[key] = value
    if not out:
        raise ValueError("No supported fields to update; allowed: " + ", ".join(_UPDATABLE_FIELDS))
    return out


# ── read tools ───────────────────────────────────────────────────────


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


# ── write tools ──────────────────────────────────────────────────────


def _write_client(user_id: Optional[str]):
    """(client, error-json) for a write: credentials must exist and still be allowed to write."""
    if not user_id:
        return None, _ERR_NO_USER
    creds = _get_servicenow_credentials(user_id)
    if not creds:
        return None, _ERR_NOT_CONNECTED
    if (creds.get("capabilities") or {}).get("can_write_incidents") is False:
        return None, json.dumps({
            "error": "The connected ServiceNow account cannot write incidents (a previous write was refused). "
                     "Reconnect with an account that has the itil role."
        })
    try:
        return ServiceNowClient.from_credentials(creds), None
    except ValueError as exc:
        return None, json.dumps({"error": str(exc)})


def servicenow_add_work_note(
    incident: str,
    note: str,
    customer_visible: bool = False,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    **kwargs,
) -> str:
    """Post a work note (or a customer-visible comment) on a ServiceNow incident."""
    _ = session_id
    if not user_id:
        raise ValueError("user_id is required to post a ServiceNow note")
    _require_servicenow_write(user_id)

    text = str(note or "").strip()[:_MAX_NOTE]
    if not text:
        raise ValueError("note is required")
    client, error = _write_client(user_id)
    if error:
        return error
    try:
        sys_id = _resolve_sys_id(client, incident)
        if not sys_id:
            return json.dumps({"error": f"Incident {_clean(incident, 64)} not found"})
        result = client.add_work_note(sys_id, text, customer_visible=bool(customer_visible))
    except (ServiceNowAPIError, ValueError) as exc:
        logger.warning("[SERVICENOW-TOOL] Work note failed for user %s: %s", user_id, exc)
        _forget_write_access_on_403(user_id, exc)
        return json.dumps({"error": f"Failed to post the note: {exc}. Continue using other tools."})
    return json.dumps({
        "status": "success",
        "number": result.get("number") or _clean(incident, 64),
        "sys_id": sys_id,
        "field": "comments" if customer_visible else "work_notes",
        "url": incident_url(client.instance_url, sys_id),
    }, ensure_ascii=False)


def servicenow_create_incident(
    short_description: str,
    description: str = "",
    priority: Optional[int] = None,
    impact: Optional[int] = None,
    urgency: Optional[int] = None,
    category: Optional[str] = None,
    assignment_group: Optional[str] = None,
    caller: Optional[str] = None,
    cmdb_ci: Optional[str] = None,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    **kwargs,
) -> str:
    """Create a ServiceNow incident."""
    _ = session_id
    if not user_id:
        raise ValueError("user_id is required to create a ServiceNow incident")
    _require_servicenow_write(user_id)
    _require_servicenow_full_write_mode(user_id)

    fields: Dict[str, Any] = {
        "short_description": str(short_description or "").strip()[:160],
        "description": str(description or "")[:_MAX_NOTE],
        "priority": _choice(priority, 1, 5),
        "impact": _choice(impact, 1, 3),
        "urgency": _choice(urgency, 1, 3),
        "category": _clean(category, 80) if category else None,
        "assignment_group": _clean(assignment_group, 80) if assignment_group else None,
        "caller_id": _clean(caller, 80) if caller else None,
        "cmdb_ci": _clean(cmdb_ci, 80) if cmdb_ci else None,
    }
    if not fields["short_description"]:
        raise ValueError("short_description is required")
    client, error = _write_client(user_id)
    if error:
        return error
    try:
        result = client.create_incident(fields)
    except (ServiceNowAPIError, ValueError) as exc:
        logger.warning("[SERVICENOW-TOOL] Create incident failed for user %s: %s", user_id, exc)
        _forget_write_access_on_403(user_id, exc)
        return json.dumps({
            "error": f"Failed to create the incident: {exc}",
            "hint": "Check the category, assignment group and caller names exist on the instance, "
                    "then retry or post a work note on an existing incident instead.",
        })
    sys_id = str(result.get("sys_id") or "")
    return json.dumps({
        "status": "success",
        "number": result.get("number"),
        "sys_id": sys_id or None,
        "state": result.get("state"),
        "priority": result.get("priority"),
        "url": incident_url(client.instance_url, sys_id) if sys_id else None,
    }, ensure_ascii=False)


def servicenow_update_incident(
    incident: str,
    fields: Optional[Dict[str, Any]] = None,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    **kwargs,
) -> str:
    """Update fields on a ServiceNow incident (state, priority, assignment, close fields, notes)."""
    _ = session_id
    if not user_id:
        raise ValueError("user_id is required to update a ServiceNow incident")
    if not fields:
        raise ValueError("fields dict is required")
    _require_servicenow_write(user_id)
    _require_servicenow_full_write_mode(user_id)

    body = _prepare_update_fields(fields)
    client, error = _write_client(user_id)
    if error:
        return error
    try:
        sys_id = _resolve_sys_id(client, incident)
        if not sys_id:
            return json.dumps({"error": f"Incident {_clean(incident, 64)} not found"})
        result = client.update_incident(sys_id, body)
    except (ServiceNowAPIError, ValueError) as exc:
        logger.warning("[SERVICENOW-TOOL] Update incident failed for user %s: %s", user_id, exc)
        _forget_write_access_on_403(user_id, exc)
        return json.dumps({
            "error": f"Failed to update the incident: {exc}",
            "hint": "Resolving needs close_code and close_notes; reference fields need names that exist.",
        })
    return json.dumps({
        "status": "success",
        "number": result.get("number") or _clean(incident, 64),
        "sys_id": sys_id,
        "updated": sorted(body.keys()),
        "state": result.get("state"),
        "url": incident_url(client.instance_url, sys_id),
    }, ensure_ascii=False)
