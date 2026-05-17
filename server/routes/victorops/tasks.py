"""Celery tasks for Splunk On-Call (VictorOps) webhook processing."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from celery_config import celery_app
from services.correlation.alert_correlator import AlertCorrelator
from services.correlation import handle_correlated_alert
from utils.auth.stateless_auth import set_rls_context

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Payload normalisation helpers
# ---------------------------------------------------------------------------

_VO_PHASE_MAP = {
    "UNACKED": "TRIGGERED",
    "ACKED": "ACKNOWLEDGED",
    "RESOLVED": "RESOLVED",
    "CRITICAL": "TRIGGERED",
    "WARNING": "TRIGGERED",
    "ACKNOWLEDGEMENT": "ACKNOWLEDGED",
    "RECOVERY": "RESOLVED",
    "INFO": "TRIGGERED",
}


def _normalize_phase(payload: Dict[str, Any]) -> str:
    """Extract and normalise the alert phase from a VictorOps flat payload."""
    raw = (
        payload.get("STATE.CURRENT_ALERT_PHASE")
        or payload.get("INCIDENT.CURRENT_PHASE")
        or payload.get("ALERT.message_type")
        or payload.get("CURRENT_ALERT_PHASE")
        or "TRIGGERED"
    ).upper()
    return _VO_PHASE_MAP.get(raw, raw)


def _extract_severity(payload: Dict[str, Any]) -> str:
    """Map Splunk On-Call entity state / message to Aurora severity."""
    entity_state = (
        payload.get("INCIDENT.ENTITY_STATE")
        or payload.get("STATE.CURRENT_STATE")
        or payload.get("ALERT.entity_state")
        or payload.get("ENTITY_STATE")
        or ""
    ).lower()
    message = (
        payload.get("ALERT.state_message")
        or payload.get("ALERT.message")
        or payload.get("STATE_MESSAGE")
        or ""
    ).lower()

    if entity_state == "critical" or any(k in message for k in ("critical", "sev1", "p1")):
        return "critical"
    if entity_state == "warning" or any(k in message for k in ("high", "sev2", "p2", "warning")):
        return "high"
    if any(k in message for k in ("medium", "sev3", "p3")):
        return "medium"
    return "medium"


def _extract_status(alert_phase: str) -> str:
    if alert_phase.upper() == "RESOLVED":
        return "resolved"
    if alert_phase.upper() == "ACKNOWLEDGED":
        return "acknowledged"
    return "investigating"


def _extract_incident_number(payload: Dict[str, Any]) -> Optional[int]:
    """Return a stable integer identifier for the incident."""
    for key in ("INCIDENT.INCIDENT_ID", "INCIDENT_NUMBER", "STATE.INCIDENT_NAME"):
        raw = payload.get(key)
        if raw is not None:
            try:
                return int(raw)
            except (ValueError, TypeError):
                pass
    return None


# ---------------------------------------------------------------------------
# Celery task
# ---------------------------------------------------------------------------

@celery_app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    name="victorops.process_event",
)
def process_victorops_event(
    self,
    payload: Dict[str, Any],
    metadata: Optional[Dict[str, Any]] = None,
    user_id: Optional[str] = None,
) -> None:
    """Background processor for Splunk On-Call outbound webhook events.

    Args:
        payload:  Full webhook JSON body from Splunk On-Call.
        metadata: Request metadata (headers, IP, etc.).
        user_id:  Aurora user ID.
    """
    received_at = datetime.now(timezone.utc)

    try:
        if not user_id:
            return

        from utils.db.connection_pool import db_pool

        alert_phase = _normalize_phase(payload)
        incident_number = _extract_incident_number(payload)
        incident_title = (
            payload.get("INCIDENT.ENTITY_DISPLAY_NAME")
            or payload.get("ALERT.entity_display_name")
            or payload.get("ALERT.title")
            or payload.get("INCIDENT_DISPLAY_NAME")
            or payload.get("ENTITY_DISPLAY_NAME")
            or payload.get("ENTITY_ID")
            or "Untitled Incident"
        )
        service_name = (
            payload.get("INCIDENT.SERVICE")
            or payload.get("ALERT.monitoring_tool")
            or payload.get("ALERT.entity_display_name")
            or payload.get("SERVICE")
            or payload.get("MONITORING_TOOL")
            or "unknown"
        )
        entity_id = (
            payload.get("STATE.ENTITY_ID")
            or payload.get("ALERT.entity_id")
            or payload.get("ENTITY_ID")
            or ""
        )
        incident_url = payload.get("ALERT.alert_url") or payload.get("INCIDENT_URL", "")

        if not incident_number:
            logger.warning(
                "[VICTOROPS] No INCIDENT_NUMBER in payload for user %s, skipping", user_id
            )
            return

        severity = _extract_severity(payload)
        aurora_status = _extract_status(alert_phase)

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                org_id = set_rls_context(cursor, conn, user_id, log_prefix="[VICTOROPS]")
                if not org_id:
                    return

                # Persist raw event
                cursor.execute(
                    """
                    INSERT INTO victorops_events
                    (user_id, org_id, alert_phase, incident_number, incident_title,
                     service_name, entity_id, payload, received_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        user_id,
                        org_id,
                        alert_phase,
                        incident_number,
                        incident_title,
                        service_name,
                        entity_id,
                        json.dumps(payload),
                        received_at,
                    ),
                )
                event_result = cursor.fetchone()
                event_db_id = event_result[0] if event_result else None
                conn.commit()

                if not event_db_id:
                    return

                alert_metadata = {
                    "incidentNumber": incident_number,
                    "incidentUrl": incident_url,
                    "entityId": entity_id,
                    "alertPhase": alert_phase,
                }
                state_message = (
                    payload.get("ALERT.state_message")
                    or payload.get("ALERT.message")
                    or payload.get("STATE_MESSAGE")
                    or ""
                )
                if state_message:
                    alert_metadata["description"] = state_message[:2000]

                # Correlation check for triggered events
                if alert_phase == "TRIGGERED":
                    try:
                        correlator = AlertCorrelator()
                        correlation_result = correlator.correlate(
                            cursor=cursor,
                            user_id=user_id,
                            source_type="victorops",
                            source_alert_id=event_db_id,
                            alert_title=incident_title,
                            alert_service=service_name,
                            alert_severity=severity,
                            alert_metadata=alert_metadata,
                            org_id=org_id,
                        )

                        if correlation_result.is_correlated:
                            handle_correlated_alert(
                                cursor=cursor,
                                user_id=user_id,
                                incident_id=correlation_result.incident_id,
                                source_type="victorops",
                                source_alert_id=event_db_id,
                                alert_title=incident_title,
                                alert_service=service_name,
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
                            "[VICTOROPS] Correlation check failed, proceeding normally: %s",
                            corr_exc,
                        )

                # Upsert incident
                cursor.execute(
                    """
                    WITH prev AS (
                        SELECT status FROM incidents
                        WHERE org_id = %s AND source_type = 'victorops'
                          AND source_alert_id = %s AND user_id = %s
                    )
                    INSERT INTO incidents
                    (user_id, org_id, source_type, source_alert_id, alert_title, alert_service,
                     severity, status, started_at, alert_metadata, alert_fired_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (org_id, source_type, source_alert_id, user_id) DO UPDATE
                    SET updated_at = CURRENT_TIMESTAMP,
                        status = EXCLUDED.status,
                        severity = EXCLUDED.severity,
                        started_at = CASE
                            WHEN incidents.status = 'resolved' AND EXCLUDED.status != 'resolved'
                            THEN EXCLUDED.started_at
                            ELSE incidents.started_at
                        END,
                        alert_metadata = EXCLUDED.alert_metadata,
                        alert_fired_at = COALESCE(EXCLUDED.alert_fired_at, incidents.alert_fired_at)
                    RETURNING id, (xmax = 0) AS inserted, (SELECT status FROM prev) AS previous_status
                    """,
                    (
                        org_id,
                        incident_number,
                        user_id,
                        user_id,
                        org_id,
                        "victorops",
                        incident_number,
                        incident_title,
                        service_name,
                        severity,
                        aurora_status,
                        received_at,
                        json.dumps(alert_metadata),
                        received_at,
                    ),
                )
                incident_row = cursor.fetchone()
                incident_db_id = incident_row[0] if incident_row else None
                incident_was_inserted = bool(incident_row[1]) if incident_row else False
                previous_status = incident_row[2] if incident_row else None
                conn.commit()

                if not incident_db_id:
                    return

                # Record primary alert for triggered events
                if alert_phase == "TRIGGERED":
                    try:
                        cursor.execute(
                            """INSERT INTO incident_alerts
                               (user_id, org_id, incident_id, source_type, source_alert_id,
                                alert_title, alert_service, alert_severity,
                                correlation_strategy, correlation_score, alert_metadata)
                               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                            (
                                user_id,
                                org_id,
                                incident_db_id,
                                "victorops",
                                event_db_id,
                                incident_title,
                                service_name,
                                severity,
                                "primary",
                                1.0,
                                json.dumps(alert_metadata),
                            ),
                        )
                        cursor.execute(
                            "UPDATE incidents SET affected_services = ARRAY[%s] WHERE id = %s",
                            (service_name, incident_db_id),
                        )
                        conn.commit()
                    except Exception as e:
                        logger.warning("[VICTOROPS] Failed to record primary alert: %s", e)

                # Lifecycle events
                lifecycle_writes = []
                if incident_was_inserted and alert_phase == "TRIGGERED":
                    lifecycle_writes.append(("created", None, "investigating"))
                elif previous_status is not None and previous_status != aurora_status:
                    ev_name = "resolved" if aurora_status == "resolved" else "status_changed"
                    lifecycle_writes.append((ev_name, previous_status, aurora_status))

                for ev_type, prev_val, new_val in lifecycle_writes:
                    try:
                        cursor.execute("SAVEPOINT sp_lifecycle")
                        cursor.execute(
                            """INSERT INTO incident_lifecycle_events
                               (incident_id, user_id, org_id, event_type, previous_value, new_value)
                               VALUES (%s, %s, %s, %s, %s, %s)""",
                            (incident_db_id, user_id, org_id, ev_type, prev_val, new_val),
                        )
                        cursor.execute("RELEASE SAVEPOINT sp_lifecycle")
                        conn.commit()
                    except Exception as e:
                        try:
                            cursor.execute("ROLLBACK TO SAVEPOINT sp_lifecycle")
                        except Exception:
                            pass
                        logger.warning(
                            "[VICTOROPS] Failed to record lifecycle event %s for incident %s: %s",
                            ev_type, incident_db_id, e,
                        )

                # SSE broadcast
                try:
                    from routes.incidents_sse import broadcast_incident_update_to_user_connections
                    broadcast_incident_update_to_user_connections(
                        user_id,
                        {"type": "incident_update", "incident_id": str(incident_db_id), "source": "victorops"},
                    )
                except Exception as e:
                    logger.warning("[VICTOROPS] Failed to notify SSE: %s", e)

                # Summary + RCA only for new triggered incidents
                if alert_phase == "TRIGGERED" and incident_was_inserted:
                    try:
                        from chat.background.summarization import generate_incident_summary
                        generate_incident_summary.delay(
                            incident_id=str(incident_db_id),
                            user_id=user_id,
                            source_type="victorops",
                            alert_title=incident_title or "Unknown Incident",
                            severity=severity,
                            service=service_name,
                            raw_payload=payload,
                            alert_metadata=alert_metadata,
                        )
                    except Exception as e:
                        logger.warning("[VICTOROPS] Failed to schedule summary: %s", e)

                    try:
                        from chat.background.task import (
                            run_background_chat,
                            create_background_chat_session,
                            is_background_chat_allowed,
                        )
                        from chat.background.rca_prompt_builder import build_victorops_rca_prompt

                        if is_background_chat_allowed(user_id):
                            rca_prompt, _rail_text = build_victorops_rca_prompt(payload, user_id=user_id)
                            session_id = create_background_chat_session(
                                user_id=user_id,
                                title=f"RCA: {incident_title}",
                                trigger_metadata={
                                    "source": "victorops",
                                    "incident_number": incident_number,
                                    "severity": severity,
                                },
                                incident_id=incident_db_id,
                            )
                            task = run_background_chat.delay(
                                user_id=user_id,
                                session_id=session_id,
                                initial_message=rca_prompt,
                                trigger_metadata={
                                    "source": "victorops",
                                    "incident_number": incident_number,
                                },
                                incident_id=incident_db_id,
                            )
                            cursor.execute(
                                "UPDATE incidents SET rca_celery_task_id = %s WHERE id = %s",
                                (task.id, incident_db_id),
                            )
                            conn.commit()
                            logger.info(
                                "[VICTOROPS] Triggered RCA for incident %s (task=%s)",
                                incident_db_id, task.id,
                            )
                    except Exception as e:
                        logger.warning("[VICTOROPS] Failed to trigger RCA: %s", e)

                logger.info(
                    "[VICTOROPS] Processed %s event for incident #%s (db_id=%s)",
                    alert_phase, incident_number, incident_db_id,
                )

    except Exception as exc:
        raise self.retry(exc=exc)
