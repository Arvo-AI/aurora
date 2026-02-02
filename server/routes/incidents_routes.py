"""API routes for incidents management."""

import json
import logging
from datetime import timezone
from flask import Blueprint, jsonify, request
from utils.db.connection_pool import db_pool
from utils.auth.stateless_auth import get_user_id_from_request
from chat.background.task import run_background_chat
from typing import List, Dict, Any, Optional
from uuid import UUID
from chat.background.task import create_background_chat_session, run_background_chat

logger = logging.getLogger(__name__)

TITLE_MAX_LENGTH = 100

incidents_bp = Blueprint("incidents", __name__)

# Maximum length for chat session titles (in characters)
TITLE_MAX_LENGTH = 50


def _format_timestamp(ts) -> Optional[str]:
    """Format timestamp ensuring UTC timezone."""
    if not ts:
        return None
    # If naive datetime, assume it's UTC (PostgreSQL TIMESTAMP without timezone)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.isoformat()


def _validate_uuid(value: str) -> bool:
    """Validate that a string is a valid UUID."""
    try:
        UUID(value)
        return True
    except (ValueError, TypeError):
        return False


def _get_user_id() -> Optional[str]:
    """Extract user_id from request (supports cookies, headers, etc)."""
    return get_user_id_from_request()


def _parse_suggestion_id(suggestion_id: str) -> Optional[int]:
    """Parse and validate a suggestion ID string to int."""
    try:
        return int(suggestion_id)
    except (ValueError, TypeError):
        return None


def _build_source_url(source_type: str, user_id: str) -> str:
    """Build platform URL from user's integration settings."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT client_id FROM user_tokens WHERE user_id=%s AND provider=%s",
                    (user_id, source_type),
                )
                row = cursor.fetchone()
                client_id = row[0] if row else None

        if source_type == "netdata":
            return "https://app.netdata.cloud"
        elif source_type == "datadog":
            return (
                f"https://app.{client_id}" if client_id else "https://app.datadoghq.com"
            )
        elif source_type == "grafana":
            return client_id if client_id else "https://grafana.com"
    except Exception as e:
        logger.error(f"[INCIDENTS] Failed to build source URL for {source_type}: {e}")
    return ""


def _format_incident_response(
    row: tuple, include_metadata: bool = False, include_correlation: bool = False
) -> Dict[str, Any]:
    """Format database row into incident response object."""
    if include_correlation:
        (
            incident_id,
            user_id,
            source_type,
            source_alert_id,
            status,
            severity,
            alert_title,
            alert_service,
            alert_environment,
            aurora_status,
            aurora_summary,
            aurora_chat_session_id,
            started_at,
            analyzed_at,
            active_tab,
            created_at,
            updated_at,
            alert_metadata,
            correlated_alert_count,
            affected_services,
        ) = row
    elif include_metadata:
        (
            incident_id,
            user_id,
            source_type,
            source_alert_id,
            status,
            severity,
            alert_title,
            alert_service,
            alert_environment,
            aurora_status,
            aurora_summary,
            aurora_chat_session_id,
            started_at,
            analyzed_at,
            active_tab,
            created_at,
            updated_at,
            alert_metadata,
        ) = row
        correlated_alert_count = None
        affected_services = None
    else:
        (
            incident_id,
            user_id,
            source_type,
            source_alert_id,
            status,
            severity,
            alert_title,
            alert_service,
            alert_environment,
            aurora_status,
            aurora_summary,
            aurora_chat_session_id,
            started_at,
            analyzed_at,
            active_tab,
            created_at,
            updated_at,
        ) = row
        alert_metadata = None
        correlated_alert_count = None
        affected_services = None

    result = {
        "id": str(incident_id),
        "sourceType": source_type,
        "sourceAlertId": source_alert_id,
        "status": status,
        "severity": severity,
        "alert": {
            "title": alert_title,
            "service": alert_service or "unknown",
            "source": source_type,
            "sourceUrl": _build_source_url(source_type, user_id),
        },
        "auroraStatus": aurora_status or "idle",
        "summary": aurora_summary or "",
        "chatSessionId": str(aurora_chat_session_id)
        if aurora_chat_session_id
        else None,
        "activeTab": active_tab or "thoughts",
        "startedAt": _format_timestamp(started_at),
        "analyzedAt": _format_timestamp(analyzed_at),
        "createdAt": _format_timestamp(created_at),
        "updatedAt": _format_timestamp(updated_at),
    }

    # Add metadata fields to alert object if available
    if alert_metadata and isinstance(alert_metadata, dict):
        result["alert"]["metadata"] = alert_metadata

    # Add correlation fields if available
    if correlated_alert_count is not None:
        result["correlatedAlertCount"] = correlated_alert_count
    if affected_services is not None:
        result["affectedServices"] = (
            affected_services if isinstance(affected_services, list) else []
        )

    return result


@incidents_bp.route("/api/incidents", methods=["GET"])
def get_incidents():
    """Get all incidents for the current user."""
    user_id = _get_user_id()
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                # Set RLS context
                cursor.execute("SET myapp.current_user_id = %s", (user_id,))

                cursor.execute(
                    """
                    SELECT 
                        id, user_id, source_type, source_alert_id, status, severity,
                        alert_title, alert_service, alert_environment, aurora_status, aurora_summary,
                        aurora_chat_session_id, started_at, analyzed_at, active_tab, created_at, updated_at,
                        alert_metadata, correlated_alert_count, affected_services
                    FROM incidents
                    WHERE user_id = %s
                    ORDER BY started_at DESC
                    LIMIT 100
                    """,
                    (user_id,),
                )
                rows = cursor.fetchall()

                incidents = [
                    _format_incident_response(
                        row, include_metadata=True, include_correlation=True
                    )
                    for row in rows
                ]

                logger.info(
                    "[INCIDENTS] Retrieved %d incidents for user %s",
                    len(incidents),
                    user_id,
                )
                return jsonify({"incidents": incidents}), 200

    except Exception as exc:
        logger.exception(
            "[INCIDENTS] Failed to retrieve incidents for user %s", user_id
        )
        return jsonify({"error": "Failed to retrieve incidents"}), 500


@incidents_bp.route("/api/incidents/<incident_id>", methods=["GET"])
def get_incident(incident_id: str):
    """Get a specific incident with suggestions and thoughts."""
    user_id = _get_user_id()
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400

    # Validate incident_id is a valid UUID
    if not _validate_uuid(incident_id):
        return jsonify({"error": "Invalid incident ID format"}), 400

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                # Set RLS context
                cursor.execute("SET myapp.current_user_id = %s", (user_id,))
                # Get incident details
                cursor.execute(
                    """
                    SELECT 
                        id, user_id, source_type, source_alert_id, status, severity,
                        alert_title, alert_service, alert_environment, aurora_status, aurora_summary,
                        aurora_chat_session_id, started_at, analyzed_at, active_tab, created_at, updated_at,
                        alert_metadata, correlated_alert_count, affected_services
                    FROM incidents
                    WHERE id = %s AND user_id = %s
                    """,
                    (incident_id, user_id),
                )
                row = cursor.fetchone()

                if not row:
                    return jsonify({"error": "Incident not found"}), 404

                incident = _format_incident_response(
                    row, include_metadata=True, include_correlation=True
                )

                # Fetch raw alert data from source table
                source_type = incident["sourceType"]
                source_alert_id = incident["sourceAlertId"]
                raw_payload = None

                logger.debug(
                    "[INCIDENTS] Fetching raw payload for incident %s: source_type=%s, source_alert_id=%s",
                    incident_id,
                    source_type,
                    source_alert_id,
                )

                if source_type == "netdata":
                    # For netdata, source_alert_id might be composite (name:host:chart) or integer
                    # Try to parse as integer for old records, skip payload fetch for composite keys
                    try:
                        alert_id_int = int(source_alert_id)
                        cursor.execute(
                            "SELECT payload FROM netdata_alerts WHERE id = %s AND user_id = %s",
                            (alert_id_int, user_id),
                        )
                        alert_row = cursor.fetchone()
                        if alert_row and alert_row[0] is not None:
                            raw_payload = alert_row[0]
                            logger.debug(
                                "[INCIDENTS] Found Netdata payload: type=%s, has_data=%s",
                                type(raw_payload).__name__,
                                bool(raw_payload),
                            )
                    except (ValueError, TypeError):
                        logger.debug(
                            "[INCIDENTS] Skipping payload fetch for composite netdata alert_id: %s",
                            source_alert_id,
                        )
                elif source_type == "grafana":
                    # For grafana, try integer lookup for old records
                    try:
                        alert_id_int = int(source_alert_id)
                        cursor.execute(
                            "SELECT payload FROM grafana_alerts WHERE id = %s AND user_id = %s",
                            (alert_id_int, user_id),
                        )
                        alert_row = cursor.fetchone()
                        if alert_row:
                            raw_payload = (
                                alert_row[0] if alert_row[0] is not None else None
                            )
                            logger.debug(
                                "[INCIDENTS] Found Grafana payload: type=%s, has_data=%s",
                                type(raw_payload).__name__ if raw_payload else None,
                                bool(raw_payload),
                            )
                    except (ValueError, TypeError):
                        logger.debug(
                            "[INCIDENTS] Skipping payload fetch for grafana fingerprint: %s",
                            source_alert_id,
                        )
                elif source_type == "datadog":
                    # For datadog, try integer lookup for old records
                    try:
                        alert_id_int = int(source_alert_id)
                        cursor.execute(
                            "SELECT payload FROM datadog_events WHERE id = %s AND user_id = %s",
                            (alert_id_int, user_id),
                        )
                        alert_row = cursor.fetchone()
                        if alert_row and alert_row[0] is not None:
                            raw_payload = alert_row[0]
                            logger.debug(
                                "[INCIDENTS] Found Datadog payload: type=%s, has_data=%s",
                                type(raw_payload).__name__,
                                bool(raw_payload),
                            )
                    except (ValueError, TypeError):
                        logger.debug(
                            "[INCIDENTS] Skipping payload fetch for datadog alert_id: %s",
                            source_alert_id,
                        )
                elif source_type == "pagerduty":
                    # Use incident_id from alert_metadata to query pagerduty_events
                    pagerduty_incident_id = (
                        incident.get("alert", {}).get("metadata", {}).get("incidentId")
                    )
                    if pagerduty_incident_id:
                        try:
                            # Fetch and consolidate ALL events for this incident using shared utility
                            from routes.pagerduty.runbook_utils import (
                                fetch_and_consolidate_pagerduty_events,
                            )

                            consolidated = fetch_and_consolidate_pagerduty_events(
                                user_id, pagerduty_incident_id, cursor
                            )
                            raw_payload = (
                                json.dumps(consolidated)
                                if consolidated and not isinstance(consolidated, str)
                                else consolidated
                            )
                            logger.debug(
                                "[INCIDENTS] Found consolidated PagerDuty payload: has_data=%s",
                                bool(raw_payload),
                            )
                        except (ValueError, TypeError) as e:
                            logger.debug(
                                "[INCIDENTS] Error fetching PagerDuty payload: %s", e
                            )
                elif source_type == "splunk":
                    # For Splunk, source_alert_id is the splunk_alerts table id (integer)
                    try:
                        alert_id_int = int(source_alert_id)
                        cursor.execute(
                            "SELECT payload FROM splunk_alerts WHERE id = %s AND user_id = %s",
                            (alert_id_int, user_id),
                        )
                        alert_row = cursor.fetchone()
                        if alert_row and alert_row[0] is not None:
                            raw_payload = alert_row[0]
                            logger.debug(
                                "[INCIDENTS] Found Splunk payload: type=%s, has_data=%s",
                                type(raw_payload).__name__,
                                bool(raw_payload),
                            )
                    except (ValueError, TypeError):
                        logger.debug(
                            "[INCIDENTS] Skipping payload fetch for splunk alert_id: %s",
                            source_alert_id,
                        )

                # Log warning if no payload found for any source type
                if not raw_payload:
                    logger.warning(
                        "[INCIDENTS] No payload found for incident %s (source_type=%s, source_alert_id=%s)",
                        incident_id,
                        source_type,
                        source_alert_id,
                    )

                # Add raw payload to alert object (sourceUrl already set by _format_incident_response)
                if raw_payload:
                    if isinstance(raw_payload, str):
                        try:
                            # If it's a string, parse and reformat for pretty printing
                            incident["alert"]["rawPayload"] = json.dumps(
                                json.loads(raw_payload), indent=2
                            )
                        except (json.JSONDecodeError, TypeError):
                            # If parsing fails, use as-is
                            incident["alert"]["rawPayload"] = raw_payload
                    else:
                        # JSONB returns as dict/list, format it
                        incident["alert"]["rawPayload"] = json.dumps(
                            raw_payload, indent=2
                        )
                else:
                    incident["alert"]["rawPayload"] = ""

                incident["alert"]["triggeredAt"] = incident["startedAt"]

                logger.debug(
                    "[INCIDENTS] Incident %s: rawPayload length=%d, sourceUrl=%s",
                    incident_id,
                    len(incident["alert"]["rawPayload"]),
                    incident["alert"].get("sourceUrl", ""),
                )

                cursor.execute(
                    """SELECT id, source_type, alert_title, alert_service, alert_severity,
                              correlation_strategy, correlation_score, correlation_details, received_at
                       FROM incident_alerts
                       WHERE incident_id = %s
                       ORDER BY received_at ASC""",
                    (incident_id,),
                )
                alert_rows = cursor.fetchall()
                correlated_alerts = []
                for arow in alert_rows:
                    correlated_alerts.append(
                        {
                            "id": str(arow[0]),
                            "sourceType": arow[1],
                            "alertTitle": arow[2],
                            "alertService": arow[3],
                            "alertSeverity": arow[4],
                            "correlationStrategy": arow[5],
                            "correlationScore": arow[6],
                            "correlationDetails": arow[7]
                            if isinstance(arow[7], dict)
                            else {},
                            "receivedAt": _format_timestamp(arow[8]),
                        }
                    )
                incident["correlatedAlerts"] = correlated_alerts

                # Get suggestions (including fix-type fields)
                cursor.execute(
                    """
                    SELECT id, incident_id, title, description, type, risk, command, created_at,
                           file_path, original_content, suggested_content, user_edited_content,
                           repository, pr_url, pr_number, created_branch, applied_at
                    FROM incident_suggestions
                    WHERE incident_id = %s
                    ORDER BY created_at ASC
                    """,
                    (incident_id,),
                )
                suggestion_rows = cursor.fetchall()

                suggestions = []
                for srow in suggestion_rows:
                    suggestion = {
                        "id": str(srow[0]),
                        "title": srow[2],
                        "description": srow[3],
                        "type": srow[4] or "diagnostic",
                        "risk": srow[5] or "safe",
                        "command": srow[6],
                        "createdAt": _format_timestamp(srow[7]),
                    }
                    # Add fix-type fields if present
                    if srow[4] == "fix":
                        suggestion.update(
                            {
                                "filePath": srow[8],
                                "originalContent": srow[9],
                                "suggestedContent": srow[10],
                                "userEditedContent": srow[11],
                                "repository": srow[12],
                                "prUrl": srow[13],
                                "prNumber": srow[14],
                                "createdBranch": srow[15],
                                "appliedAt": _format_timestamp(srow[16]),
                            }
                        )
                    suggestions.append(suggestion)

                # Get thoughts
                cursor.execute(
                    """
                    SELECT id, incident_id, timestamp, content, thought_type, created_at
                    FROM incident_thoughts
                    WHERE incident_id = %s
                    ORDER BY timestamp ASC
                    """,
                    (incident_id,),
                )
                thought_rows = cursor.fetchall()

                thoughts = []
                for trow in thought_rows:
                    thoughts.append(
                        {
                            "id": str(trow[0]),
                            "timestamp": _format_timestamp(trow[2]),
                            "content": trow[3],
                            "type": trow[4] or "analysis",
                            "createdAt": _format_timestamp(trow[5]),
                        }
                    )

                # Get citations (with safe ordering - filter to numeric keys only)
                try:
                    cursor.execute(
                        """
                        SELECT id, citation_key, tool_name, command, output, executed_at, created_at
                        FROM incident_citations
                        WHERE incident_id = %s
                          AND citation_key ~ '^[0-9]+$'
                        ORDER BY citation_key::int ASC
                        """,
                        (incident_id,),
                    )
                    citation_rows = cursor.fetchall()
                except Exception as citation_err:
                    logger.warning(
                        "[INCIDENTS] Failed to fetch citations for %s: %s",
                        incident_id,
                        citation_err,
                    )
                    citation_rows = []

                citations = []
                for crow in citation_rows:
                    citations.append(
                        {
                            "id": str(crow[0]),
                            "key": crow[1],
                            "toolName": crow[2] or "Unknown",
                            "command": crow[3] or "",
                            "output": crow[4] or "",
                            "executedAt": _format_timestamp(crow[5]),
                            "createdAt": _format_timestamp(crow[6]),
                        }
                    )

                # Get all chat sessions linked to this incident
                try:
                    cursor.execute(
                        """
                        SELECT id, title, messages, status, created_at, updated_at
                        FROM chat_sessions
                        WHERE incident_id = %s AND user_id = %s AND is_active = true
                        ORDER BY created_at ASC
                        """,
                        (incident_id, user_id),
                    )
                    chat_session_rows = cursor.fetchall()
                except Exception as chat_err:
                    logger.warning(
                        "[INCIDENTS] Failed to fetch chat sessions for %s: %s",
                        incident_id,
                        chat_err,
                    )
                    chat_session_rows = []

                chat_sessions = []
                for csrow in chat_session_rows:
                    chat_sessions.append(
                        {
                            "id": csrow[0],
                            "title": csrow[1],
                            "messages": csrow[2] if csrow[2] else [],
                            "status": csrow[3] or "active",
                            "createdAt": _format_timestamp(csrow[4]),
                            "updatedAt": _format_timestamp(csrow[5]),
                        }
                    )

                incident["suggestions"] = suggestions
                incident["streamingThoughts"] = thoughts
                incident["citations"] = citations
                incident["chatSessions"] = chat_sessions

                logger.info(
                    "[INCIDENTS] Retrieved incident %s for user %s with %d suggestions, %d thoughts, %d citations, %d chat sessions",
                    incident_id,
                    user_id,
                    len(suggestions),
                    len(thoughts),
                    len(citations),
                    len(chat_sessions),
                )
                return jsonify({"incident": incident}), 200

    except Exception as exc:
        logger.exception("[INCIDENTS] Failed to retrieve incident for user %s", user_id)
        return jsonify({"error": "Failed to retrieve incident"}), 500


@incidents_bp.route("/api/incidents/<incident_id>/alerts", methods=["GET"])
def get_incident_alerts(incident_id: str):
    """Get all correlated alerts for a specific incident."""
    user_id = _get_user_id()
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400

    if not _validate_uuid(incident_id):
        return jsonify({"error": "Invalid incident ID format"}), 400

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SET myapp.current_user_id = %s", (user_id,))

                cursor.execute(
                    "SELECT 1 FROM incidents WHERE id = %s AND user_id = %s",
                    (incident_id, user_id),
                )
                if not cursor.fetchone():
                    return jsonify({"error": "Incident not found"}), 404

                cursor.execute(
                    """SELECT id, source_type, alert_title, alert_service, alert_severity,
                              correlation_strategy, correlation_score, correlation_details, received_at
                       FROM incident_alerts
                       WHERE incident_id = %s
                       ORDER BY received_at ASC""",
                    (incident_id,),
                )
                alert_rows = cursor.fetchall()

                alerts = []
                for arow in alert_rows:
                    alerts.append(
                        {
                            "id": str(arow[0]),
                            "sourceType": arow[1],
                            "alertTitle": arow[2],
                            "alertService": arow[3],
                            "alertSeverity": arow[4],
                            "correlationStrategy": arow[5],
                            "correlationScore": arow[6],
                            "correlationDetails": arow[7]
                            if isinstance(arow[7], dict)
                            else {},
                            "receivedAt": _format_timestamp(arow[8]),
                        }
                    )

                logger.info(
                    "[INCIDENTS] Retrieved %d alerts for incident %s",
                    len(alerts),
                    incident_id,
                )
                return jsonify({"alerts": alerts, "total": len(alerts)}), 200

    except Exception as exc:
        logger.exception(
            "[INCIDENTS] Failed to retrieve alerts for incident %s", incident_id
        )
        return jsonify({"error": "Failed to retrieve alerts"}), 500


# Allowed values for validation
ALLOWED_INCIDENT_STATUS = {"investigating", "analyzed"}
ALLOWED_AURORA_STATUS = {"idle", "running", "complete", "error"}
ALLOWED_ACTIVE_TAB = {"thoughts", "chat"}


@incidents_bp.route("/api/incidents/<incident_id>", methods=["PATCH"])
def update_incident(incident_id: str):
    """Update incident status or other fields."""
    user_id = _get_user_id()
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400

    # Validate incident_id is a valid UUID
    if not _validate_uuid(incident_id):
        return jsonify({"error": "Invalid incident ID format"}), 400

    data = request.get_json()
    if not data:
        return jsonify({"error": "Missing request body"}), 400

    # Validate input fields
    if "status" in data and data["status"] not in ALLOWED_INCIDENT_STATUS:
        return jsonify(
            {
                "error": f"Invalid status. Must be one of: {', '.join(ALLOWED_INCIDENT_STATUS)}"
            }
        ), 400

    if "auroraStatus" in data and data["auroraStatus"] not in ALLOWED_AURORA_STATUS:
        return jsonify(
            {
                "error": f"Invalid auroraStatus. Must be one of: {', '.join(ALLOWED_AURORA_STATUS)}"
            }
        ), 400

    if "activeTab" in data and data["activeTab"] not in ALLOWED_ACTIVE_TAB:
        return jsonify(
            {
                "error": f"Invalid activeTab. Must be one of: {', '.join(ALLOWED_ACTIVE_TAB)}"
            }
        ), 400

    if "summary" in data and len(str(data["summary"])) > 10000:
        return jsonify({"error": "Summary too long (max 10000 characters)"}), 400

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                # Set RLS context
                cursor.execute("SET myapp.current_user_id = %s", (user_id,))
                # Build update query dynamically based on provided fields
                update_fields = []
                values = []

                if "status" in data:
                    update_fields.append("status = %s")
                    values.append(data["status"])
                    # Auto-set timestamps based on status
                    if data["status"] == "analyzed" and "analyzed_at" not in data:
                        update_fields.append("analyzed_at = CURRENT_TIMESTAMP")

                if "auroraStatus" in data:
                    update_fields.append("aurora_status = %s")
                    values.append(data["auroraStatus"])

                if "summary" in data:
                    update_fields.append("aurora_summary = %s")
                    values.append(data["summary"])

                if "activeTab" in data:
                    update_fields.append("active_tab = %s")
                    values.append(data["activeTab"])

                if not update_fields:
                    return jsonify({"error": "No valid fields to update"}), 400

                # Always update updated_at
                update_fields.append("updated_at = CURRENT_TIMESTAMP")

                # Add WHERE clause values
                values.extend([incident_id, user_id])

                query = f"""
                    UPDATE incidents
                    SET {", ".join(update_fields)}
                    WHERE id = %s AND user_id = %s
                    RETURNING id
                """

                cursor.execute(query, values)
                result = cursor.fetchone()

                if not result:
                    return jsonify({"error": "Incident not found"}), 404

                conn.commit()

                logger.info(
                    "[INCIDENTS] Updated incident %s for user %s", incident_id, user_id
                )
                return jsonify({"success": True, "id": str(result[0])}), 200

    except Exception as exc:
        logger.exception("[INCIDENTS] Failed to update incident for user %s", user_id)
        return jsonify({"error": "Failed to update incident"}), 500


@incidents_bp.route("/api/incidents/<incident_id>/chat", methods=["POST"])
def incident_chat(incident_id: str):
    """Ask a question about an ongoing incident investigation using background chat task.

    Query params:
    - session_id (optional): If provided, continues an existing chat session
    """
    user_id = _get_user_id()
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400

    if not _validate_uuid(incident_id):
        return jsonify({"error": "Invalid incident ID format"}), 400

    data = request.get_json()
    if not data or not data.get("question"):
        return jsonify({"error": "Missing question"}), 400

    question = data["question"].strip()
    if len(question) > 2000:
        return jsonify({"error": "Question too long (max 2000 characters)"}), 400

    # Get mode parameter (default to "ask" for read-only, "agent" for execution)
    mode = data.get("mode", "ask")
    if mode not in ("ask", "agent"):
        return jsonify({"error": 'Invalid mode. Must be "ask" or "agent"'}), 400

    # Check for session_id in query params
    existing_session_id = request.args.get("session_id")
    logger.info(
        "[INCIDENTS] Received chat request for incident %s: question=%s, existing_session_id=%s",
        incident_id,
        question[:TITLE_MAX_LENGTH],
        existing_session_id,
    )

    if existing_session_id and not _validate_uuid(existing_session_id):
        return jsonify({"error": "Invalid session ID format"}), 400

    try:
        # Determine if we're continuing an existing session or creating a new one
        if existing_session_id:
            # Validate session belongs to this user and is linked to this incident.
            # Sessions can link to incidents in two ways:
            #   1. chat_sessions.incident_id - for follow-up Q&A chats
            #   2. incidents.aurora_chat_session_id - for the original RCA session
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SET myapp.current_user_id = %s", (user_id,))
                    cursor.execute(
                        """
                        SELECT cs.id
                        FROM chat_sessions cs
                        WHERE cs.id = %s
                          AND cs.user_id = %s
                          AND (
                            cs.incident_id = %s
                            OR EXISTS (
                              SELECT 1 FROM incidents i
                              WHERE i.id = %s AND i.aurora_chat_session_id = cs.id::uuid
                            )
                          )
                        """,
                        (existing_session_id, user_id, incident_id, incident_id),
                    )
                    session_row = cursor.fetchone()

                    if not session_row:
                        return jsonify(
                            {
                                "error": "Session not found or does not belong to this incident"
                            }
                        ), 404

                    # Update session status to in_progress
                    cursor.execute(
                        "UPDATE chat_sessions SET status = %s WHERE id = %s",
                        ("in_progress", existing_session_id),
                    )
                    conn.commit()

            # Use existing session - just send the question without full context
            session_id = existing_session_id
            full_message = question
            is_new_session = False
            logger.info(
                "[INCIDENTS] Continuing existing session %s for incident %s",
                session_id,
                incident_id,
            )

        else:
            # Create new session - fetch incident details and thoughts for context
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SET myapp.current_user_id = %s", (user_id,))

                    # Get incident
                    cursor.execute(
                        """
                        SELECT alert_title, alert_service, severity, aurora_summary, aurora_status
                        FROM incidents
                        WHERE id = %s AND user_id = %s
                        """,
                        (incident_id, user_id),
                    )
                    incident_row = cursor.fetchone()

                    if not incident_row:
                        return jsonify({"error": "Incident not found"}), 404

                    alert_title, alert_service, severity, summary, aurora_status = (
                        incident_row
                    )

                    # Get investigation thoughts
                    cursor.execute(
                        """
                        SELECT content, thought_type, timestamp
                        FROM incident_thoughts
                        WHERE incident_id = %s
                        ORDER BY timestamp ASC
                        LIMIT 50
                        """,
                        (incident_id,),
                    )
                    thought_rows = cursor.fetchall()

                    # Build thoughts context
                    thoughts_list = []
                    for row in thought_rows:
                        timestamp_str = row[2].strftime("%H:%M:%S") if row[2] else "N/A"
                        thoughts_list.append(f"[{timestamp_str}] {row[0]}")

            # Build context message with clear structure for the LLM
            context_prefix = f"""<context>
<incident>
Title: {alert_title}
Service: {alert_service or "Unknown"}
Severity: {severity}
Status: {aurora_status or "Unknown"}
Current Summary: {summary or "No summary yet"}
</incident>

<investigation_progress>
{chr(10).join(thoughts_list) if thoughts_list else "No investigation thoughts recorded yet."}
</investigation_progress>
</context>

<instructions>
You are having a conversation with a user about the incident above. Follow these rules:

1. If the user is just greeting you or asking a simple question (e.g., "hi", "what's the summary?", "what happened?"):
   → Respond conversationally and briefly. DO NOT start investigating.

2. If the user is explicitly asking you to investigate something specific (e.g., "check the database logs", "investigate the API", "look at service X"):
   → Acknowledge their request and explain what you would investigate and why, based on the context above.
   → You are in READ-ONLY mode, so describe your investigation approach rather than executing commands.

3. If the user is providing hints or context (e.g., "I think it's related to X", "this might be a database issue"):
   → Acknowledge their insight and explain how it connects to the investigation so far.
   → Suggest what should be investigated next based on their hint.

KEY: Do NOT automatically start a full investigation unless explicitly asked. Default to conversational responses.
</instructions>

<user_message>
{question}
</user_message>"""

            full_message = context_prefix

            # Generate title from question
            title = (
                f"Incident: {question[:TITLE_MAX_LENGTH]}..."
                if len(question) > TITLE_MAX_LENGTH
                else f"Incident: {question}"
            )

            # Create session with incident metadata
            trigger_metadata = {
                "source": "incident_chat",
                "incident_id": incident_id,
                "question": question,
            }

            session_id = create_background_chat_session(
                user_id=user_id,
                title=title,
                trigger_metadata=trigger_metadata,
                incident_id=incident_id,  # Link session to incident for retrieval
            )
            is_new_session = True
            logger.info(
                "[INCIDENTS] Created new session %s for incident %s",
                session_id,
                incident_id,
            )

        # Launch background chat task
        # DON'T pass incident_id to run_background_chat - it would treat this as an RCA investigation
        # The incident_id is stored in chat_sessions table for retrieval, not for RCA workflow
        trigger_metadata = {
            "source": "incident_chat",
            "incident_id": incident_id,
            "question": question,
        }

        run_background_chat.delay(
            user_id=user_id,
            session_id=session_id,
            initial_message=full_message,
            trigger_metadata=trigger_metadata,
            provider_preference=None,  # Use default
            incident_id=None,  # Don't trigger RCA workflow - this is a Q&A chat
            send_notifications=False,  # No notifications for Q&A
            mode=mode,  # Pass mode for execution capability
        )

        logger.info(
            "[INCIDENTS] Background chat task queued for incident %s, session %s (new=%s, mode=%s)",
            incident_id,
            session_id,
            is_new_session,
            mode,
        )
        return jsonify(
            {
                "session_id": session_id,
                "status": "processing",
                "is_new_session": is_new_session,
            }
        ), 202  # 202 Accepted

    except Exception as exc:
        logger.exception(
            "[INCIDENTS] Failed to process chat for incident %s", incident_id
        )
        return jsonify({"error": "Failed to process question"}), 500


@incidents_bp.route("/api/incidents/suggestions/<suggestion_id>", methods=["PATCH"])
def update_suggestion(suggestion_id: str):
    """Update a fix suggestion (edit content, approve, reject)."""
    user_id = _get_user_id()
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400

    suggestion_id_int = _parse_suggestion_id(suggestion_id)
    if suggestion_id_int is None:
        return jsonify({"error": "Invalid suggestion ID"}), 400

    data = request.get_json() or {}
    user_edited_content = data.get("userEditedContent")
    if not user_edited_content or not user_edited_content.strip():
        return jsonify({"error": "No changes provided (content cannot be empty)"}), 400

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT s.id FROM incident_suggestions s
                    JOIN incidents i ON s.incident_id = i.id
                    WHERE s.id = %s AND i.user_id = %s AND s.type = 'fix'
                    """,
                    (suggestion_id_int, user_id),
                )
                if not cursor.fetchone():
                    return jsonify(
                        {"error": "Suggestion not found or access denied"}
                    ), 404

                cursor.execute(
                    "UPDATE incident_suggestions SET user_edited_content = %s WHERE id = %s",
                    (user_edited_content, suggestion_id_int),
                )
                conn.commit()

        logger.info(
            "[INCIDENTS] Updated suggestion %s for user %s", suggestion_id, user_id
        )
        return jsonify({"success": True, "message": "Suggestion updated"}), 200

    except Exception as exc:
        logger.exception("[INCIDENTS] Failed to update suggestion %s", suggestion_id)
        return jsonify({"error": "Failed to update suggestion"}), 500


@incidents_bp.route(
    "/api/incidents/suggestions/<suggestion_id>/apply", methods=["POST"]
)
def apply_fix_suggestion(suggestion_id: str):
    """Apply a fix suggestion by creating a branch and PR."""
    user_id = _get_user_id()
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400

    suggestion_id_int = _parse_suggestion_id(suggestion_id)
    if suggestion_id_int is None:
        return jsonify({"error": "Invalid suggestion ID"}), 400

    data = request.get_json() or {}
    use_edited_content = data.get("useEditedContent", True)
    target_branch = data.get("targetBranch")

    try:
        from chat.backend.agent.tools.github_apply_fix_tool import github_apply_fix

        result_json = github_apply_fix(
            suggestion_id=suggestion_id_int,
            use_edited_content=use_edited_content,
            target_branch=target_branch,
            user_id=user_id,
        )
        result = json.loads(result_json)

        if result.get("success"):
            logger.info(
                "[INCIDENTS] Applied fix suggestion %s, PR: %s",
                suggestion_id,
                result.get("pr_url"),
            )
            return jsonify(result), 200

        logger.warning(
            "[INCIDENTS] Failed to apply fix suggestion %s: %s",
            suggestion_id,
            result.get("error"),
        )
        return jsonify(result), 400

    except Exception as exc:
        logger.exception("[INCIDENTS] Failed to apply fix suggestion %s", suggestion_id)
        return jsonify({"error": "Failed to apply fix suggestion"}), 500
