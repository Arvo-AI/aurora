"""
Recent Incidents Tool

Retrieves recent completed incident investigations for situational awareness
during RCA. Lets the agent see what Aurora has already investigated recently,
enabling it to independently notice overlapping root causes.
"""

import json
import logging
from typing import Any, Optional

from pydantic import BaseModel, Field

from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

_SUMMARY_TRUNCATION_CHARS = 500


class GetRecentIncidentsArgs(BaseModel):
    time_window_hours: int = Field(
        default=72,
        ge=1,
        le=168,
        description="How far back to look in hours (default: 72h / 3 days)",
    )
    limit: int = Field(
        default=20,
        ge=1,
        le=50,
        description="Maximum number of incidents to return",
    )
    service_filter: Optional[str] = Field(
        default=None,
        description="Optional service name to filter by (matches alert_service or affected_services)",
    )


GET_RECENT_INCIDENTS_DESCRIPTION = (
    "List recent incidents that Aurora has already investigated. "
    "Returns alert titles, affected services, severity, and root cause summaries. "
    "Available for situational awareness during investigation."
)


def get_recent_incidents(
    time_window_hours: int = 72,
    limit: int = 20,
    service_filter: Optional[str] = None,
    user_id: Optional[str] = None,
    incident_id: Optional[str] = None,
    **kwargs: Any,  # Accepted for LangChain tool injection compatibility
) -> str:
    """Retrieve recent analyzed incidents for situational awareness."""
    if not user_id:
        return json.dumps({"error": "User authentication required."})

    if not incident_id:
        return json.dumps({"error": "No incident context. This tool is only available during RCA investigations."})

    from utils.db.connection_pool import db_pool

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                from utils.auth.stateless_auth import set_rls_context

                org_id = set_rls_context(cursor, conn, user_id, log_prefix="[GetRecentIncidents]")
                if not org_id:
                    return json.dumps({"error": "Failed to resolve organization context."})

                params = []
                conditions = [
                    "status = 'analyzed'",
                    "id != %s",
                    "started_at >= NOW() - INTERVAL '%s hours'",
                ]
                params.append(incident_id)
                params.append(time_window_hours)

                if service_filter:
                    conditions.append(
                        "(alert_service ILIKE %s OR %s = ANY(affected_services))"
                    )
                    params.append(f"%{service_filter}%")
                    params.append(service_filter)

                query = f"""
                    SELECT id, alert_title, alert_service, severity,
                           aurora_summary, started_at, affected_services,
                           correlated_alert_count
                    FROM incidents
                    WHERE {' AND '.join(conditions)}
                    ORDER BY started_at DESC
                    LIMIT %s
                """
                params.append(limit)

                cursor.execute(query, params)
                rows = cursor.fetchall()

                if not rows:
                    return json.dumps({"incidents": [], "count": 0})

                incidents = []
                for row in rows:
                    summary = row[4] or ""
                    if len(summary) > _SUMMARY_TRUNCATION_CHARS:
                        summary = summary[:_SUMMARY_TRUNCATION_CHARS] + "..."

                    incidents.append({
                        "id": str(row[0]),
                        "alert_title": row[1],
                        "alert_service": row[2],
                        "severity": row[3],
                        "aurora_summary": summary,
                        "started_at": row[5].isoformat() if row[5] else None,
                        "affected_services": row[6] or [],
                        "correlated_alert_count": row[7] or 0,
                    })

                return json.dumps({"incidents": incidents, "count": len(incidents)})

    except Exception as e:
        logger.exception("[GetRecentIncidents] Error retrieving recent incidents: %s", sanitize(e))
        return json.dumps({"error": f"Error retrieving recent incidents: {sanitize(e)}"})
