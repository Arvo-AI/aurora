"""
Trigger RCA Tool

LLM-callable tool that bridges interactive chat with the background RCA pipeline.
When the user describes an operational incident in chat, the LLM calls this tool
to create an incident and dispatch a full automated RCA investigation using all
connected integrations.
"""

import json
import logging
import zlib
from datetime import datetime
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class TriggerRCAArgs(BaseModel):
    """Arguments for triggering an RCA investigation from chat."""

    issue_description: str = Field(
        description="What the user described — the operational issue or symptoms they're seeing"
    )
    title: str = Field(
        default="",
        description="Short incident title, e.g. 'High CPU on prod API servers'",
    )
    service: str = Field(
        default="",
        description="Affected service if identifiable from the user's message",
    )
    severity: str = Field(
        default="medium",
        description="Inferred severity: critical, high, medium, or low",
    )


def trigger_rca(
    issue_description: str,
    title: str = "",
    service: str = "",
    severity: str = "medium",
    user_id: str | None = None,
    session_id: str | None = None,
    **kwargs,
) -> str:
    """
    Trigger a full background RCA investigation from an interactive chat session.

    Creates an incident, broadcasts an SSE update, and dispatches the background
    RCA pipeline (same one used by webhook-triggered alerts) with all connected
    integrations auto-discovered.

    Args:
        issue_description: What the user described
        title: Short incident title (LLM-derived)
        service: Affected service if identifiable
        severity: critical/high/medium/low
        user_id: Injected by context wrapper
        session_id: Injected by context wrapper

    Returns:
        JSON string with incident_id and session_id, or error message
    """
    if not user_id:
        return json.dumps({"error": "No user context available. Cannot trigger RCA."})

    issue_description = (issue_description or "").strip()
    if not issue_description:
        return json.dumps({"error": "issue_description must be non-empty."})

    try:
        from chat.backend.agent.tools.cloud_tools import get_state_context
        state = get_state_context()
        if state and getattr(state, "is_background", False):
            return json.dumps({
                "error": "Cannot trigger RCA from within a background RCA session. "
                "This tool is only available in interactive chat."
            })
    except Exception as e:
        logger.warning(f"[TriggerRCA] Could not check background state, allowing: {e}")

    try:
        from chat.background.task import is_background_chat_allowed
        if not is_background_chat_allowed(user_id):
            return json.dumps({
                "error": "Rate limited — too many RCA investigations in the last 5 minutes. "
                "Please wait a moment and try again."
            })
    except Exception as e:
        logger.warning(f"[TriggerRCA] Rate limit check failed: {e}")
        return json.dumps({
            "error": "Rate limit check unavailable — please try again shortly."
        })

    incident_title = (title or f"User-reported: {issue_description[:80]}")[:200]
    severity = severity.lower() if severity else "medium"
    if severity not in ("critical", "high", "medium", "low"):
        severity = "medium"

    now = datetime.now()
    timestamp_str = now.isoformat()
    source_alert_id = zlib.crc32(
        f"{timestamp_str}:{user_id}:{issue_description}".encode()
    ) & 0x7FFFFFFF

    alert_metadata = {
        "user_description": issue_description,
        "service": service,
        "triggered_from_session": session_id,
        "triggered_at": timestamp_str,
    }

    from utils.auth.stateless_auth import get_org_id_for_user
    from utils.db.connection_pool import db_pool

    org_id = get_org_id_for_user(user_id)
    if not org_id:
        return json.dumps({"error": "Could not resolve organization for user."})

    incident_id = None
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO incidents
                       (user_id, org_id, source_type, source_alert_id, alert_title,
                        alert_service, severity, status, started_at, alert_metadata)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (org_id, source_type, source_alert_id, user_id) DO UPDATE
                       SET updated_at = CURRENT_TIMESTAMP,
                           alert_metadata = EXCLUDED.alert_metadata
                       RETURNING id""",
                    (
                        user_id, org_id, "chat", source_alert_id, incident_title,
                        service or "unknown", severity, "investigating",
                        now, json.dumps(alert_metadata),
                    ),
                )
                incident_row = cursor.fetchone()
                incident_id = str(incident_row[0]) if incident_row else None
            conn.commit()
    except Exception as e:
        logger.exception(f"[TriggerRCA] Failed to create incident: {e}")
        return json.dumps({"error": f"Failed to create incident: {e}"})

    if not incident_id:
        return json.dumps({"error": "Failed to create incident — no ID returned"})

    logger.info(
        f"[TriggerRCA] Created incident {incident_id} for user {user_id} "
        f"(source_alert_id={source_alert_id})"
    )

    try:
        from routes.incidents_sse import broadcast_incident_update_to_user_connections
        broadcast_incident_update_to_user_connections(
            user_id,
            {"type": "incident_update", "incident_id": incident_id, "source": "chat"},
            org_id=org_id,
        )
    except Exception as e:
        logger.warning(f"[TriggerRCA] Failed to broadcast SSE: {e}")

    try:
        from chat.background.summarization import generate_incident_summary
        generate_incident_summary.delay(
            incident_id=incident_id, user_id=user_id, source_type="chat",
            alert_title=incident_title, severity=severity,
            service=service or "unknown",
            raw_payload=alert_metadata, alert_metadata=alert_metadata,
        )
    except Exception as e:
        logger.warning(f"[TriggerRCA] Failed to enqueue summary: {e}")

    rca_session_id = None
    try:
        from chat.background.task import (
            create_background_chat_session,
            run_background_chat,
        )
        from chat.background.rca_prompt_builder import build_chat_rca_prompt

        trigger_metadata = {"source": "chat", "incident_id": incident_id}

        chat_title = f"RCA: {incident_title}"
        rca_session_id = create_background_chat_session(
            user_id=user_id, title=chat_title,
            trigger_metadata=trigger_metadata, incident_id=incident_id,
        )

        rca_prompt = build_chat_rca_prompt(
            description=issue_description, title=incident_title,
            service=service, severity=severity, user_id=user_id,
        )

        task = run_background_chat.delay(
            user_id=user_id, session_id=rca_session_id,
            initial_message=rca_prompt, trigger_metadata=trigger_metadata,
            incident_id=incident_id,
        )

        try:
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "UPDATE incidents SET rca_celery_task_id = %s WHERE id = %s",
                        (task.id, incident_id),
                    )
                conn.commit()
        except Exception as e:
            logger.warning(f"[TriggerRCA] Failed to store task ID on incident: {e}")

        logger.info(
            f"[TriggerRCA] Dispatched background RCA session {rca_session_id} "
            f"(task_id={task.id}) for incident {incident_id}"
        )

    except Exception as e:
        logger.exception(f"[TriggerRCA] Failed to dispatch background RCA: {e}")
        try:
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "UPDATE incidents SET status = 'failed' WHERE id = %s",
                        (incident_id,),
                    )
                conn.commit()
        except Exception:
            logger.warning(f"[TriggerRCA] Could not mark incident {incident_id} as failed")
        return json.dumps({
            "incident_id": incident_id,
            "error": f"Incident created but RCA dispatch failed: {e}",
        })

    return json.dumps({
        "status": "ok",
        "incident_id": incident_id,
        "rca_session_id": rca_session_id,
        "message": (
            "Incident created and full RCA investigation started. "
            "The investigation will run in the background using all connected integrations. "
            "You can track progress in the Incidents dashboard."
        ),
    })
