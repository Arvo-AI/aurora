"""
Alert Payload Drill-Down Tool

Retrieves full (untruncated) field values from the stored webhook payload.
Used by the RCA agent when the initial prompt contained a truncated payload
and the agent needs to inspect a specific field in full.
"""

import json
import logging
from typing import Optional

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


def get_alert_field(
    json_path: str,
    user_id: Optional[str] = None,
    incident_id: Optional[str] = None,
    **kwargs,
) -> str:
    """Retrieve a specific field from the stored webhook payload."""
    if not user_id:
        return "Error: User authentication required."
    if not incident_id:
        return "Error: No incident context. This tool is only available during RCA investigations."
    if not json_path or not json_path.strip():
        return "Error: json_path is required. Provide a dot-separated path like 'event.incident.summary'."

    from utils.db.connection_pool import db_pool
    from utils.auth.stateless_auth import set_rls_context

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix="[GetAlertField]")

                cursor.execute(
                    "SELECT source_type, source_alert_id FROM incidents WHERE id = %s",
                    (incident_id,),
                )
                row = cursor.fetchone()
                if not row:
                    return f"Error: Incident {incident_id} not found."

                source_type, source_alert_id = row[0], row[1]
                table = _SOURCE_TABLE_MAP.get(source_type)
                if not table:
                    return f"Error: Unknown source type '{source_type}'. Cannot look up payload."

                # Try direct ID lookup first
                cursor.execute(
                    f"SELECT payload FROM {table} WHERE id = %s",
                    (source_alert_id,),
                )
                payload_row = cursor.fetchone()

                # Fallback: query by user + most recent (handles providers where
                # source_alert_id is not the event table row ID, e.g. Grafana CRC)
                if not payload_row or not payload_row[0]:
                    cursor.execute("SELECT org_id FROM incidents WHERE id = %s", (incident_id,))
                    org_row = cursor.fetchone()
                    if org_row:
                        cursor.execute(
                            f"SELECT payload FROM {table} WHERE user_id = %s AND org_id = %s "
                            f"ORDER BY received_at DESC LIMIT 1",
                            (user_id, org_row[0]),
                        )
                        payload_row = cursor.fetchone()

                if not payload_row or not payload_row[0]:
                    return f"Error: No payload found in {table} for source_alert_id {source_alert_id}."

                payload = payload_row[0]
                if isinstance(payload, str):
                    payload = json.loads(payload)

                # Navigate the dot-separated path
                path_parts = json_path.strip().split(".")
                current = payload
                for part in path_parts:
                    if current is None:
                        return f"Error: Path '{json_path}' not found. Value is null at '{part}'."
                    if isinstance(current, dict):
                        if part in current:
                            current = current[part]
                        else:
                            available = list(current.keys())[:20]
                            return (
                                f"Error: Key '{part}' not found at this level.\n"
                                f"Available keys: {available}"
                            )
                    elif isinstance(current, list):
                        try:
                            idx = int(part)
                            if 0 <= idx < len(current):
                                current = current[idx]
                            else:
                                return f"Error: Index {idx} out of range (list has {len(current)} items)."
                        except ValueError:
                            return f"Error: Expected numeric index for list, got '{part}'."
                    else:
                        return f"Error: Cannot traverse into {type(current).__name__} at '{part}'."

                # Serialize the result
                if isinstance(current, (dict, list)):
                    result = json.dumps(current, ensure_ascii=False, default=str, indent=2)
                else:
                    result = str(current) if current is not None else "null"

                from chat.backend.constants import MAX_TOOL_OUTPUT_CHARS
                if len(result) > MAX_TOOL_OUTPUT_CHARS:
                    result = result[:MAX_TOOL_OUTPUT_CHARS] + "\n... [output truncated]"

                return result

    except Exception as e:
        logger.exception("[GetAlertField] Error retrieving field: %s", e)
        return f"Error retrieving alert field: {str(e)}"
