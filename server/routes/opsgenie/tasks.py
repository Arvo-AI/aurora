"""Celery tasks for OpsGenie webhook processing."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from celery_config import celery_app
from chat.background.rca_prompt_builder import build_opsgenie_rca_prompt
from services.correlation.alert_correlator import AlertCorrelator
from services.correlation import handle_correlated_alert

logger = logging.getLogger(__name__)

_PRIORITY_MAP = {"P1": "critical", "P2": "high", "P3": "medium", "P4": "low", "P5": "low"}


def _extract_severity(payload: Dict[str, Any]) -> str:
    """Map OpsGenie P1-P5 priority to severity."""
    alert = payload.get("alert", {})
    priority = (alert.get("priority") or "").upper()

    return _PRIORITY_MAP.get(priority, "unknown")


def _extract_service(payload: Dict[str, Any]) -> str:
    """Extract service name from OpsGenie payload.

    Checks alert.tags for ``service:xxx`` pattern first, then falls back
    to alert.source and alert.entity.
    """
    alert = payload.get("alert", {})
    tags = alert.get("tags", [])

    # Handle tags as string (comma-separated) or list
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]

    for tag in tags:
        if isinstance(tag, str) and tag.startswith("service:"):
            return tag.split(":", 1)[1][:255]

    # Fallback to source, then entity
    service = alert.get("source") or alert.get("entity")
    return str(service)[:255] if service else "unknown"


def _safe_json_dump(data: Dict[str, Any]) -> str:
    try:
        return json.dumps(data, ensure_ascii=False)
    except Exception:
        logger.debug("JSON serialization failed, falling back to str(): %s", type(data))
        return str(data)



@celery_app.task(
    bind=True, max_retries=3, default_retry_delay=30, name="opsgenie.process_event"
)
def process_opsgenie_event(
    self,
    payload: Dict[str, Any],
    metadata: Optional[Dict[str, Any]] = None,
    user_id: Optional[str] = None,
) -> None:
    """Background processor for OpsGenie webhook payloads."""
    alert = payload.get("alert", {})
    action = payload.get("action", "unknown")
    alert_message = alert.get("message", "OpsGenie Alert")
    logger.info("[OPSGENIE][WEBHOOK][USER:%s] action=%s message=%s", user_id or "unknown", action, alert_message)
    logger.debug("[OPSGENIE][WEBHOOK] payload=%s", _safe_json_dump(payload))

    try:
        if not user_id:
            logger.warning("[OPSGENIE][WEBHOOK] Missing user_id; skipping persistence")
            return

        from utils.db.connection_pool import db_pool

        alert_id = alert.get("alertId", "")
        priority = alert.get("priority", "")
        status = alert.get("status", "")
        source = alert.get("source", "")

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                from utils.auth.stateless_auth import set_rls_context
                org_id = set_rls_context(cursor, conn, user_id, log_prefix="[OPSGENIE][WEBHOOK]")
                if not org_id:
                    return

                received_at = datetime.now(timezone.utc)
                cursor.execute(
                    """
                    INSERT INTO opsgenie_events (user_id, org_id, action, alert_id, alert_message, priority, status, source, payload, received_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        user_id,
                        org_id,
                        action,
                        alert_id,
                        alert_message,
                        priority,
                        status,
                        source,
                        json.dumps(payload),
                        received_at,
                    ),
                )
                event_result = cursor.fetchone()
                event_id = event_result[0] if event_result else None
                conn.commit()

                if not event_id:
                    logger.error(
                        "[OPSGENIE][WEBHOOK] Failed to get event_id for user %s", user_id
                    )
                    return

                logger.info("[OPSGENIE][WEBHOOK] Stored event %s (action=%s) for user %s", event_id, action, user_id)

                # Only create incident + trigger RCA on alert creation
                if action.lower() not in ("create", "create alert"):
                    logger.info("[OPSGENIE][WEBHOOK] Action '%s' is not a create — skipping incident creation", action)
                    return

                # Skip auto-generated "Incident raised" alerts from JSM automation
                if alert_message.startswith("[") and "Incident raised" in alert_message:
                    logger.info("[OPSGENIE][WEBHOOK] Skipping JSM auto-generated incident alert: %s", alert_message)
                    return

                # Create incident record
                severity = _extract_severity(payload)
                service = _extract_service(payload)

                # Build alert metadata with OpsGenie-specific fields
                alert_metadata = {}
                if alert_id:
                    alert_metadata["alertId"] = alert_id
                if priority:
                    alert_metadata["priority"] = priority
                if alert.get("alias"):
                    alert_metadata["alias"] = alert.get("alias")
                if alert.get("entity"):
                    alert_metadata["entity"] = alert.get("entity")
                tags = alert.get("tags", [])
                if tags:
                    alert_metadata["tags"] = tags
                if alert.get("description"):
                    alert_metadata["description"] = alert.get("description")
                if alert.get("teams"):
                    alert_metadata["teams"] = alert.get("teams")
                source_info = payload.get("source", {})
                if isinstance(source_info, dict) and source_info.get("name"):
                    alert_metadata["sourceName"] = source_info["name"]

                try:
                    correlator = AlertCorrelator()
                    correlation_result = correlator.correlate(
                        cursor=cursor,
                        user_id=user_id,
                        source_type="opsgenie",
                        source_alert_id=event_id,
                        alert_title=alert_message,
                        alert_service=service,
                        alert_severity=severity,
                        alert_metadata=alert_metadata,
                        org_id=org_id,
                    )

                    if correlation_result.is_correlated:
                        handle_correlated_alert(
                            cursor=cursor,
                            user_id=user_id,
                            incident_id=correlation_result.incident_id,
                            source_type="opsgenie",
                            source_alert_id=event_id,
                            alert_title=alert_message,
                            alert_service=service,
                            alert_severity=severity,
                            correlation_result=correlation_result,
                            alert_metadata=alert_metadata,
                            raw_payload=payload,
                            org_id=org_id,
                        )
                        conn.commit()
                        return
                except Exception as corr_exc:
                    logger.warning(
                        "[OPSGENIE] Correlation check failed, proceeding with normal flow: %s",
                        corr_exc,
                    )

                cursor.execute(
                    """
                    INSERT INTO incidents
                    (user_id, org_id, source_type, source_alert_id, alert_title, alert_service,
                     severity, status, started_at, alert_metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (org_id, source_type, source_alert_id, user_id) DO UPDATE
                    SET updated_at = CURRENT_TIMESTAMP,
                        started_at = CASE
                            WHEN incidents.status != 'analyzed' THEN EXCLUDED.started_at
                            ELSE incidents.started_at
                        END,
                        alert_metadata = EXCLUDED.alert_metadata
                    RETURNING id
                    """,
                    (
                        user_id,
                        org_id,
                        "opsgenie",
                        event_id,
                        alert_message,
                        service,
                        severity,
                        "investigating",
                        received_at,
                        json.dumps(alert_metadata),
                    ),
                )
                incident_row = cursor.fetchone()
                incident_id = incident_row[0] if incident_row else None
                conn.commit()

                try:
                    cursor.execute(
                        """INSERT INTO incident_alerts
                           (user_id, org_id, incident_id, source_type, source_alert_id, alert_title, alert_service,
                            alert_severity, correlation_strategy, correlation_score, alert_metadata)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (
                            user_id,
                            org_id,
                            incident_id,
                            "opsgenie",
                            event_id,
                            alert_message,
                            service,
                            severity,
                            "primary",
                            1.0,
                            json.dumps(alert_metadata),
                        ),
                    )
                    cursor.execute(
                        "UPDATE incidents SET affected_services = ARRAY[%s] WHERE id = %s",
                        (service, incident_id),
                    )
                    conn.commit()
                except Exception as e:
                    logger.warning("[OPSGENIE] Failed to record primary alert: %s", e)

                if incident_id:
                    logger.info(
                        "[OPSGENIE][WEBHOOK] Created incident %s for event %s",
                        incident_id,
                        event_id,
                    )

                    # Notify SSE connections about incident update
                    try:
                        from routes.incidents_sse import (
                            broadcast_incident_update_to_user_connections,
                        )

                        broadcast_incident_update_to_user_connections(
                            user_id,
                            {
                                "type": "incident_update",
                                "incident_id": str(incident_id),
                                "source": "opsgenie",
                            },
                        )
                    except Exception as e:
                        logger.warning("[OPSGENIE][WEBHOOK] Failed to notify SSE: %s", e)

                    # Trigger summary generation
                    try:
                        from chat.background.summarization import generate_incident_summary

                        generate_incident_summary.delay(
                            incident_id=str(incident_id),
                            user_id=user_id,
                            source_type="opsgenie",
                            alert_title=alert_message or "Unknown Alert",
                            severity=severity,
                            service=service,
                            raw_payload=payload,
                            alert_metadata=alert_metadata,
                        )
                    except Exception as e:
                        logger.error("[OPSGENIE][WEBHOOK] Failed to trigger summary: %s", e)

                    # Post "RCA in progress" comment to linked JSM incident (delayed to allow automation to create it)
                    try:
                        frontend_url = os.getenv("FRONTEND_URL", "").rstrip("/")
                        aurora_link = f"{frontend_url}/incidents/{incident_id}" if frontend_url else ""
                        rca_comment = f"Aurora is investigating this alert. RCA in progress.\n\nAlert: {alert_message}"
                        if aurora_link:
                            rca_comment += f"\n\nView in Aurora: {aurora_link}"
                        post_jsm_comment.apply_async(
                            kwargs={
                                "user_id": user_id,
                                "alert_message": alert_message,
                                "comment": rca_comment,
                            },
                            countdown=10,
                        )
                    except Exception as e:
                        logger.debug("[OPSGENIE][WEBHOOK] Could not enqueue JSM comment: %s", e)

                    # Trigger background chat for RCA
                    try:
                        from chat.background.task import (
                            run_background_chat,
                            create_background_chat_session,
                            is_background_chat_allowed,
                        )

                        if not is_background_chat_allowed(user_id):
                            logger.info(
                                "[OPSGENIE][WEBHOOK] Skipping background RCA - rate limited for user %s",
                                user_id,
                            )
                        else:
                            session_id = create_background_chat_session(
                                user_id=user_id,
                                title=f"RCA: {alert_message or 'OpsGenie Alert'}",
                                trigger_metadata={
                                    "source": "opsgenie",
                                    "alert_id": alert_id,
                                    "action": action,
                                },
                                incident_id=str(incident_id),
                            )

                            rca_prompt = build_opsgenie_rca_prompt(
                                payload, user_id=user_id
                            )

                            task = run_background_chat.delay(
                                user_id=user_id,
                                session_id=session_id,
                                initial_message=rca_prompt,
                                trigger_metadata={
                                    "source": "opsgenie",
                                    "alert_id": alert_id,
                                    "alert_title": alert_message,
                                    "action": action,
                                },
                                incident_id=str(incident_id),
                            )

                            cursor.execute(
                                "UPDATE incidents SET rca_celery_task_id = %s WHERE id = %s",
                                (task.id, str(incident_id))
                            )
                            conn.commit()

                            logger.info(
                                "[OPSGENIE][WEBHOOK] Triggered background RCA for session %s (task_id=%s)",
                                session_id,
                                task.id,
                            )
                    except Exception as e:
                        logger.error(
                            "[OPSGENIE][WEBHOOK] Failed to trigger RCA: %s", e
                        )

    except Exception as exc:
        logger.exception("[OPSGENIE][WEBHOOK] Failed to process webhook payload")
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=2, default_retry_delay=10, name="opsgenie.post_jsm_comment")
def post_jsm_comment(self, user_id: str, alert_message: str, comment: str) -> None:
    """Post a comment to the JSM incident linked to an alert. Runs with a delay to allow JSM automation to create the incident first."""
    try:
        from routes.opsgenie.opsgenie_routes import _build_client_from_creds, _get_stored_opsgenie_credentials
        creds = _get_stored_opsgenie_credentials(user_id)
        if not creds or creds.get("auth_type") != "jsm_basic":
            return
        client = _build_client_from_creds(creds)
        if not client or not hasattr(client, "find_incident_for_alert"):
            return
        issue_key = client.find_incident_for_alert(alert_message)
        if issue_key:
            client.add_comment_to_issue(issue_key, comment)
            logger.info("[OPSGENIE] Posted comment to linked JSM incident")
        else:
            logger.debug("[OPSGENIE] No JSM incident found matching alert")
    except Exception as exc:
        logger.warning("[OPSGENIE] Failed to post JSM comment: %s", exc)
        raise self.retry(exc=exc)
