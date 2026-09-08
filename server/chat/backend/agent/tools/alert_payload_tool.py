"""
Alert Payload Drill-Down Tool

Retrieves full (untruncated) field values from the stored webhook payload.
Used by the RCA agent when the initial prompt contained a truncated payload
and the agent needs to inspect a specific field in full.
"""

import json
import logging
from datetime import timedelta
from typing import Any, Optional, Tuple

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_SOURCE_TABLE_MAP = {
    "grafana": "grafana_alerts",
    "datadog": "datadog_events",
    "newrelic": "newrelic_events",
    "pagerduty": "pagerduty_events",
    "opsgenie": "opsgenie_events",
    "sentry": "sentry_events",
    "splunk": "splunk_alerts",
    "dynatrace": "dynatrace_problems",
    "bigpanda": "bigpanda_events",
    "netdata": "netdata_alerts",
    "incidentio": "incidentio_alerts",
    "jenkins": "jenkins_deployment_events",
    "cloudbees": "jenkins_deployment_events",
    "spinnaker": "spinnaker_deployment_events",
}

_PAYLOAD_LOOKUP_WINDOW = timedelta(minutes=10)


class GetAlertFieldArgs(BaseModel):
    json_path: str = Field(
        description=(
            "Dot-separated path to the field in the webhook payload. "
            "Use numeric indices for arrays. "
            "Examples: 'alerts.0.labels', 'event.incident.summary', 'results.0'"
        )
    )


GET_ALERT_FIELD_DESCRIPTION = (
    "Retrieve the full (untruncated) value of a field from the original webhook payload. "
    "Use when the RCA prompt shows a truncated field you need to inspect fully. "
    "Provide a dot-separated JSON path (e.g. 'event.incident.custom_field_entries', 'alerts.0.annotations')."
)


def _validate_inputs(json_path: str, user_id: Optional[str], incident_id: Optional[str]) -> Optional[str]:
    """Validate required inputs; returns an error string or None if valid."""
    if not user_id:
        return "Error: User authentication required."
    if not incident_id:
        return "Error: No incident context. This tool is only available during RCA investigations."
    if not json_path or not json_path.strip():
        return "Error: json_path is required. Provide a dot-separated path like 'event.incident.summary'."
    return None


def _fetch_payload(cursor, conn, incident_id: str, user_id: str) -> Tuple[Optional[Any], Optional[str]]:
    """Fetch the raw payload for an incident. Returns (payload_dict, error_string)."""
    from utils.auth.stateless_auth import set_rls_context

    set_rls_context(cursor, conn, user_id, log_prefix="[GetAlertField]")

    cursor.execute(
        "SELECT source_type, source_alert_id, created_at FROM incidents WHERE id = %s",
        (incident_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None, f"Error: Incident {incident_id} not found."

    source_type, source_alert_id, incident_created_at = row[0], row[1], row[2]
    table = _SOURCE_TABLE_MAP.get(source_type)
    if not table:
        return _fetch_from_alert_metadata(cursor, incident_id, source_type)

    payload_row = _lookup_payload_row(
        cursor, table, source_alert_id, user_id, incident_created_at
    )
    if not payload_row or not payload_row[0]:
        return None, f"Error: No payload found in {table} for source_alert_id {source_alert_id}."

    payload = payload_row[0]
    if isinstance(payload, str):
        payload = json.loads(payload)

    return payload, None


def _fetch_from_alert_metadata(
    cursor, incident_id: str, source_type: str
) -> Tuple[Optional[Any], Optional[str]]:
    """Fallback for sources with no dedicated events table: read alert_metadata."""
    cursor.execute("SELECT alert_metadata FROM incidents WHERE id = %s", (incident_id,))
    meta_row = cursor.fetchone()
    if meta_row and meta_row[0]:
        payload = meta_row[0] if isinstance(meta_row[0], dict) else json.loads(meta_row[0])
        return payload, None
    return None, f"Error: No alert data found for source type '{source_type}'."


def _lookup_payload_row(cursor, table: str, source_alert_id, user_id: str, incident_created_at):
    """Find the payload row by direct id, falling back to a time-window lookup
    (fail-closed when the window is ambiguous)."""
    cursor.execute(f"SELECT payload FROM {table} WHERE id = %s", (source_alert_id,))
    payload_row = cursor.fetchone()
    if (payload_row and payload_row[0]) or not incident_created_at:
        return payload_row

    window_start = incident_created_at - _PAYLOAD_LOOKUP_WINDOW
    window_end = incident_created_at + _PAYLOAD_LOOKUP_WINDOW
    cursor.execute(
        f"SELECT payload FROM {table} WHERE user_id = %s AND received_at BETWEEN %s AND %s",
        (user_id, window_start, window_end),
    )
    rows = cursor.fetchall()
    return rows[0] if len(rows) == 1 else None


def _traverse_path(payload: Any, json_path: str) -> Tuple[Optional[Any], Optional[str]]:
    """Walk a dot-separated path through the payload. Returns (value, error_string)."""
    path_parts = json_path.strip().split(".")
    current = payload
    for part in path_parts:
        if current is None:
            return None, f"Error: Path '{json_path}' not found. Value is null at '{part}'."
        if isinstance(current, dict):
            if part in current:
                current = current[part]
            else:
                available = list(current.keys())[:20]
                return None, (
                    f"Error: Key '{part}' not found at this level.\n"
                    f"Available keys: {available}"
                )
        elif isinstance(current, list):
            try:
                idx = int(part)
            except ValueError:
                return None, f"Error: Expected numeric index for list, got '{part}'."
            if 0 <= idx < len(current):
                current = current[idx]
            else:
                return None, f"Error: Index {idx} out of range (list has {len(current)} items)."
        else:
            return None, f"Error: Cannot traverse into {type(current).__name__} at '{part}'."
    return current, None


def _format_output(value: Any) -> str:
    """Serialize and truncate the extracted value for tool output."""
    from chat.backend.constants import MAX_TOOL_OUTPUT_CHARS

    if isinstance(value, (dict, list)):
        result = json.dumps(value, ensure_ascii=False, default=str, indent=2)
    else:
        result = str(value) if value is not None else "null"

    if len(result) > MAX_TOOL_OUTPUT_CHARS:
        result = result[:MAX_TOOL_OUTPUT_CHARS] + "\n... [output truncated]"
    return result


def get_alert_field(
    json_path: str,
    user_id: Optional[str] = None,
    incident_id: Optional[str] = None,
    **kwargs,
) -> str:
    """Retrieve a specific field from the stored webhook payload."""
    error = _validate_inputs(json_path, user_id, incident_id)
    if error:
        return error

    from utils.db.connection_pool import db_pool

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                payload, err = _fetch_payload(cursor, conn, incident_id, user_id)
                if err:
                    return err

                value, err = _traverse_path(payload, json_path)
                if err:
                    return err

                return _format_output(value)

    except Exception as e:
        logger.exception("[GetAlertField] Error retrieving field: %s", e)
        return f"Error retrieving alert field: {e}"
