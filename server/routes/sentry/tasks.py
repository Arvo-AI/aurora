"""Celery tasks for Sentry integrations."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from celery_config import celery_app
from chat.background.rca_prompt_builder import build_rca_prompt
from chat.background.summarization import generate_incident_summary
from chat.background.task import (
    run_background_chat,
    create_background_chat_session,
    is_background_chat_allowed,
)
from routes.incidents_sse import broadcast_incident_update_to_user_connections
from services.correlation.alert_correlator import AlertCorrelator
from services.correlation import handle_correlated_alert
from utils import safe_json_dump
from utils.auth.stateless_auth import set_rls_context
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)


def _summarize_event(payload: Dict[str, Any]) -> str:
    action = payload.get("action", "unknown")
    data = payload.get("data", {})

    title = (
        data.get("event", {}).get("title")
        or data.get("metric_alert", {}).get("title")
        or data.get("issue", {}).get("title")
        or "Sentry Event"
    )

    parts = [title, f"[action={action}]"]
    triggered_rule = data.get("triggered_rule")
    if triggered_rule:
        parts.append(f"rule={triggered_rule}")
    return " ".join(parts)


def _extract_severity(payload: Dict[str, Any]) -> str:
    action = payload.get("action", "").lower()
    if action == "critical":
        return "critical"
    elif action in ("warning", "resolved"):
        return "high" if action == "warning" else "low"
    elif action == "triggered":
        data = payload.get("data", {})
        event = data.get("event", {})
        level = (event.get("level") or "").lower()
        if level == "fatal":
            return "critical"
        elif level == "error":
            return "high"
        elif level == "warning":
            return "medium"
        return "medium"
    return "unknown"


def _extract_service(payload: Dict[str, Any]) -> str:
    data = payload.get("data", {})

    # Issue alerts: service lives in event tags (tuple or dict format depending on Sentry version)
    event = data.get("event", {})
    tags = event.get("tags", [])
    if isinstance(tags, list):
        for tag in tags:
            if isinstance(tag, (list, tuple)) and len(tag) == 2:
                if tag[0] == "service":
                    return str(tag[1])[:255]
                if tag[0] == "server_name":
                    return str(tag[1])[:255]
            elif isinstance(tag, dict):
                if tag.get("key") == "service":
                    return str(tag.get("value", ""))[:255]
                if tag.get("key") == "server_name":
                    return str(tag.get("value", ""))[:255]

    # Error events: project name as plain string on the event object
    project = event.get("project")
    if project:
        return str(project)[:255]

    # Metric alerts: no event object, only metric_alert.projects[]
    metric_alert = data.get("metric_alert", {})
    projects = metric_alert.get("projects", [])
    if projects:
        return str(projects[0])[:255]

    return "unknown"


def _extract_title(payload: Dict[str, Any]) -> str:
    data = payload.get("data", {})
    return (
        data.get("event", {}).get("title")
        or data.get("metric_alert", {}).get("title")
        or data.get("issue", {}).get("title")
        or "Sentry Alert"
    )


@celery_app.task(
    bind=True, max_retries=3, default_retry_delay=30, name="sentry.process_event"
)
def process_sentry_event(
    self,
    payload: Dict[str, Any],
    metadata: Optional[Dict[str, Any]] = None,
    user_id: Optional[str] = None,
) -> None:
    """Background processor for Sentry webhook payloads."""
    summary = _summarize_event(payload)
    logger.info("[SENTRY][WEBHOOK][USER:%s] %s", user_id or "unknown", summary)
    logger.debug("[SENTRY][WEBHOOK] payload=%s", safe_json_dump(payload))

    try:
        if not user_id:
            logger.warning("[SENTRY][WEBHOOK] Missing user_id; skipping persistence")
            return

        data = payload.get("data", {})
        action = payload.get("action", "")

        event_type = "metric_alert" if "metric_alert" in data else "issue_alert"
        event_title = _extract_title(payload)
        status = action
        scope = data.get("event", {}).get("project") or (
            data.get("metric_alert", {}).get("projects", [None])[0]
            if data.get("metric_alert", {}).get("projects")
            else None
        )

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                org_id = set_rls_context(cursor, conn, user_id, log_prefix="[SENTRY][WEBHOOK]")
                if not org_id:
                    return

                received_at = datetime.now(timezone.utc)

                cursor.execute(
                    """
                    INSERT INTO sentry_events (user_id, org_id, event_type, event_title, status, scope, payload, received_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        user_id,
                        org_id,
                        event_type,
                        event_title,
                        status,
                        scope,
                        json.dumps(payload),
                        received_at,
                    ),
                )
                event_result = cursor.fetchone()
                event_id = event_result[0] if event_result else None
                conn.commit()

                if not event_id:
                    logger.error("[SENTRY][WEBHOOK] Failed to get event_id for user %s", user_id)
                    return

                logger.info("[SENTRY][WEBHOOK] Stored event for user %s", user_id)

                severity = _extract_severity(payload)
                service = _extract_service(payload)

                alert_metadata: Dict[str, Any] = {}
                if data.get("event", {}).get("event_id"):
                    alert_metadata["eventId"] = data["event"]["event_id"]
                if data.get("triggered_rule"):
                    alert_metadata["triggeredRule"] = data["triggered_rule"]
                if data.get("event", {}).get("url"):
                    alert_metadata["alertUrl"] = data["event"]["url"]
                if data.get("metric_alert", {}).get("id"):
                    alert_metadata["metricAlertId"] = str(data["metric_alert"]["id"])
                if data.get("event", {}).get("message"):
                    alert_metadata["message"] = data["event"]["message"]
                actor = payload.get("actor", {})
                if actor:
                    alert_metadata["actor"] = actor

                try:
                    correlator = AlertCorrelator()
                    correlation_result = correlator.correlate(
                        cursor=cursor,
                        user_id=user_id,
                        source_type="sentry",
                        source_alert_id=event_id,
                        alert_title=event_title,
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
                            source_type="sentry",
                            source_alert_id=event_id,
                            alert_title=event_title,
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
                        "[SENTRY] Correlation check failed, proceeding with normal flow: %s",
                        corr_exc,
                    )

                cursor.execute(
                    """
                    INSERT INTO incidents
                    (user_id, org_id, source_type, source_alert_id, alert_title, alert_service,
                     severity, status, started_at, alert_metadata, alert_fired_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (org_id, source_type, source_alert_id, user_id) DO UPDATE
                    SET updated_at = CURRENT_TIMESTAMP,
                        started_at = CASE
                            WHEN incidents.status != 'analyzed' THEN EXCLUDED.started_at
                            ELSE incidents.started_at
                        END,
                        alert_metadata = EXCLUDED.alert_metadata,
                        alert_fired_at = COALESCE(EXCLUDED.alert_fired_at, incidents.alert_fired_at)
                    RETURNING id, (xmax = 0) AS inserted
                    """,
                    (
                        user_id,
                        org_id,
                        "sentry",
                        event_id,
                        event_title,
                        service,
                        severity,
                        "investigating",
                        received_at,
                        json.dumps(alert_metadata),
                        received_at,
                    ),
                )
                incident_row = cursor.fetchone()
                incident_id = incident_row[0] if incident_row else None
                incident_was_inserted = bool(incident_row[1]) if incident_row else False
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
                            "sentry",
                            event_id,
                            event_title,
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
                    logger.warning("[SENTRY] Failed to record primary alert: %s", e)

                if incident_id and incident_was_inserted:
                    try:
                        cursor.execute("SAVEPOINT sp_incident_lifecycle")
                        cursor.execute(
                            """INSERT INTO incident_lifecycle_events
                               (incident_id, user_id, org_id, event_type, new_value)
                               VALUES (%s, %s, %s, %s, %s)""",
                            (incident_id, user_id, org_id, "created", "investigating"),
                        )
                        cursor.execute("RELEASE SAVEPOINT sp_incident_lifecycle")
                        conn.commit()
                    except Exception as e:
                        try:
                            cursor.execute("ROLLBACK TO SAVEPOINT sp_incident_lifecycle")
                        except Exception as rb_exc:
                            logger.debug(
                                "[SENTRY] Rollback to sp_incident_lifecycle failed for incident %s: %s",
                                incident_id, rb_exc,
                            )
                        logger.warning(
                            "[SENTRY] Failed to record lifecycle 'created' event for incident %s: %s",
                            incident_id, e,
                        )

                if incident_id:
                    logger.info(
                        "[SENTRY][WEBHOOK] Created incident %s for event %s",
                        incident_id,
                        event_id,
                    )

                    try:
                        broadcast_incident_update_to_user_connections(
                            user_id,
                            {
                                "type": "incident_update",
                                "incident_id": str(incident_id),
                                "source": "sentry",
                            },
                            org_id=org_id,
                        )
                    except Exception as e:
                        logger.warning("[SENTRY][WEBHOOK] Failed to notify SSE: %s", e)

                    generate_incident_summary.delay(
                        incident_id=str(incident_id),
                        user_id=user_id,
                        source_type="sentry",
                        alert_title=event_title or "Unknown Event",
                        severity=severity,
                        service=service,
                        raw_payload=payload,
                        alert_metadata=alert_metadata,
                    )

                    try:
                        if not is_background_chat_allowed(user_id):
                            logger.info(
                                "[SENTRY][WEBHOOK] Skipping background RCA - rate limited for user %s",
                                user_id,
                            )
                        else:
                            session_id = create_background_chat_session(
                                user_id=user_id,
                                title=f"RCA: {event_title or 'Sentry Alert'}",
                                trigger_metadata={
                                    "source": "sentry",
                                    "event_type": event_type,
                                    "action": action,
                                },
                                incident_id=str(incident_id),
                            )

                            alert_details = {
                                "title": event_title,
                                "severity": severity,
                                "service": service,
                                "action": action,
                                "event_type": event_type,
                                "payload": payload,
                            }
                            rca_prompt, rail_text = build_rca_prompt(
                                "sentry", alert_details, None, user_id
                            )

                            task = run_background_chat.delay(
                                user_id=user_id,
                                session_id=session_id,
                                initial_message=rca_prompt,
                                trigger_metadata={
                                    "source": "sentry",
                                    "event_type": event_type,
                                    "action": action,
                                },
                                incident_id=str(incident_id),
                                rail_text=rail_text,
                            )

                            cursor.execute(
                                "UPDATE incidents SET rca_celery_task_id = %s WHERE id = %s",
                                (task.id, str(incident_id)),
                            )
                            conn.commit()

                            logger.info(
                                "[SENTRY][WEBHOOK] Triggered background RCA for session %s (task_id=%s)",
                                session_id,
                                task.id,
                            )
                    except Exception as e:
                        logger.error("[SENTRY][WEBHOOK] Failed to trigger RCA: %s", e)

    except Exception as exc:
        logger.exception("[SENTRY][WEBHOOK] Failed to process webhook payload")
        raise self.retry(exc=exc)
