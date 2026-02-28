"""Celery tasks for BigPanda webhook processing.

Processes BigPanda incident webhooks and feeds them into Aurora's
correlation pipeline, incident creation, SSE broadcast, and RCA triggering.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from celery_config import celery_app
from services.correlation.alert_correlator import AlertCorrelator
from services.correlation import handle_correlated_alert
from utils.auth.stateless_auth import get_user_preference

logger = logging.getLogger(__name__)

_SEVERITY_MAP = {
    "critical": "critical",
    "warning": "high",
    "unknown": "unknown",
}


def _extract_severity(incident: dict[str, Any]) -> str:
    raw = str(incident.get("severity", "unknown")).lower()
    return _SEVERITY_MAP.get(raw, "medium")


def _extract_title(incident: dict[str, Any], alerts: list[dict[str, Any]]) -> str:
    first = alerts[0] if alerts else {}
    return (
        first.get("description")
        or first.get("condition_name")
        or _build_fallback_title(first, incident)
    )


def _build_fallback_title(first_alert: dict[str, Any], incident: dict[str, Any]) -> str:
    primary = first_alert.get("primary_property", "")
    secondary = first_alert.get("secondary_property", "")
    if primary and secondary:
        return f"{primary}:{secondary}"
    if primary:
        return primary
    return f"BigPanda Incident {incident.get('id', 'unknown')}"


def _extract_service(alerts: list[dict[str, Any]], incident: dict[str, Any]) -> str:
    first = alerts[0] if alerts else {}
    return str(
        first.get("primary_property")
        or first.get("source_system")
        or incident.get("incident_tags", {}).get("service", "")
        or "unknown"
    )[:255]


def _build_alert_metadata(
    incident: dict[str, Any], alerts: list[dict[str, Any]],
) -> dict[str, Any]:
    child_alerts = []
    for a in alerts[:50]:
        child_alerts.append({
            k: v for k, v in {
                "description": a.get("description"),
                "source_system": a.get("source_system"),
                "primary_property": a.get("primary_property"),
                "secondary_property": a.get("secondary_property"),
                "status": a.get("status"),
                "severity": a.get("severity"),
                "condition_name": a.get("condition_name"),
            }.items() if v
        })

    meta: dict[str, Any] = {"childAlerts": child_alerts}
    for key in ("environments", "folders", "incident_tags", "correlation_matchers_log"):
        val = incident.get(key)
        if val:
            meta[key] = val
    return meta


def _should_trigger_rca(user_id: str) -> bool:
    return get_user_preference(user_id, "bigpanda_rca_enabled", default=False)


def _build_rca_prompt(incident: dict[str, Any], alerts: list[dict[str, Any]], user_id: str | None = None) -> str:
    from chat.background.rca_prompt_builder import build_bigpanda_rca_prompt
    return build_bigpanda_rca_prompt(incident, alerts, user_id=user_id)


@celery_app.task(
    bind=True, max_retries=3, default_retry_delay=30,
    name="bigpanda.process_event",
)
def process_bigpanda_event(
    self,
    raw_payload: Dict[str, Any],
    metadata: Optional[Dict[str, Any]] = None,
    user_id: Optional[str] = None,
) -> None:
    """Process a BigPanda webhook payload through the full correlation pipeline."""
    received_at = datetime.now(timezone.utc)

    if not user_id:
        logger.warning("[BIGPANDA] Received event with no user_id, skipping")
        return

    try:
        from utils.db.connection_pool import db_pool

        incident = raw_payload
        if "incident" in raw_payload:
            incident = raw_payload["incident"]

        incident_id = incident.get("id")
        if not incident_id:
            logger.warning("[BIGPANDA] Payload missing incident ID, skipping")
            return

        bp_status = incident.get("status", "active")
        event_type = f"incident.{bp_status}"
        alerts = incident.get("alerts", [])
        first_alert = alerts[0] if alerts else {}

        title = _extract_title(incident, alerts)
        severity = _extract_severity(incident)
        service = _extract_service(alerts, incident)
        alert_metadata = _build_alert_metadata(incident, alerts)

        logger.info("[BIGPANDA][ALERT][USER:%s] %s", user_id, title)

        with db_pool.get_admin_connection() as conn, conn.cursor() as cursor:

            # 1. Store raw event in bigpanda_events
            cursor.execute(
                """INSERT INTO bigpanda_events
                   (user_id, event_type, incident_id, incident_title, incident_status,
                    incident_severity, primary_property, secondary_property,
                    source_system, child_alert_count, payload, received_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                (
                    user_id, event_type, incident_id,
                    title[:500] if title else None,
                    bp_status, incident.get("severity"),
                    first_alert.get("primary_property"),
                    first_alert.get("secondary_property"),
                    first_alert.get("source_system"),
                    len(alerts), json.dumps(raw_payload), received_at,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                logger.error("[BIGPANDA] INSERT returned no row for user %s, incident %s", user_id, incident_id)
                return
            alert_db_id = row[0]
            conn.commit()

            # 2. Run correlation
            try:
                correlator = AlertCorrelator()
                result = correlator.correlate(
                    cursor=cursor, user_id=user_id, source_type="bigpanda",
                    source_alert_id=alert_db_id, alert_title=title,
                    alert_service=service, alert_severity=severity,
                    alert_metadata=alert_metadata,
                )
                if result.is_correlated:
                    handle_correlated_alert(
                        cursor=cursor, user_id=user_id, incident_id=result.incident_id,
                        source_type="bigpanda", source_alert_id=alert_db_id,
                        alert_title=title, alert_service=service, alert_severity=severity,
                        correlation_result=result, alert_metadata=alert_metadata,
                        raw_payload=raw_payload,
                    )
                    conn.commit()
                    return
            except Exception as corr_exc:
                logger.warning("[BIGPANDA] Correlation failed, proceeding: %s", corr_exc)

            # 3. Check if RCA is enabled before creating a new incident
            if not _should_trigger_rca(user_id):
                conn.commit()
                logger.info("[BIGPANDA] Stored for user %s (RCA disabled)", user_id)
                return

            # 4. Create new incident
            cursor.execute(
                """INSERT INTO incidents
                   (user_id, source_type, source_alert_id, alert_title, alert_service,
                    severity, status, started_at, alert_metadata)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (source_type, source_alert_id, user_id) DO UPDATE
                   SET updated_at = CURRENT_TIMESTAMP,
                       started_at = CASE WHEN incidents.status != 'analyzed'
                                    THEN EXCLUDED.started_at ELSE incidents.started_at END,
                       alert_metadata = EXCLUDED.alert_metadata
                   RETURNING id""",
                (user_id, "bigpanda", alert_db_id, title, service,
                 severity, "investigating", received_at, json.dumps(alert_metadata)),
            )
            incident_row = cursor.fetchone()
            aurora_incident_id = incident_row[0] if incident_row else None

            if aurora_incident_id:
                cursor.execute(
                    """INSERT INTO incident_alerts
                       (user_id, incident_id, source_type, source_alert_id, alert_title,
                        alert_service, alert_severity, correlation_strategy, correlation_score, alert_metadata)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (user_id, aurora_incident_id, "bigpanda", alert_db_id, title,
                     service, severity, "primary", 1.0, json.dumps(alert_metadata)),
                )
                cursor.execute(
                    "UPDATE incidents SET affected_services = ARRAY[%s] WHERE id = %s",
                    (service, aurora_incident_id),
                )
            conn.commit()

        if not aurora_incident_id:
            return

        logger.info("[BIGPANDA] Created incident %s for event %s", aurora_incident_id, alert_db_id)

        # 5. Generate summary
        from chat.background.summarization import generate_incident_summary
        generate_incident_summary.delay(
            incident_id=str(aurora_incident_id), user_id=user_id, source_type="bigpanda",
            alert_title=title, severity=severity, service=service,
            raw_payload=raw_payload, alert_metadata=alert_metadata,
        )

        # 6. Trigger RCA background chat
        try:
            from chat.background.task import (
                run_background_chat, create_background_chat_session, is_background_chat_allowed,
            )
            if not is_background_chat_allowed(user_id):
                logger.info("[BIGPANDA] Skipping RCA - rate limited for user %s", user_id)
                return

            session_id = create_background_chat_session(
                user_id=user_id,
                title=f"RCA: {title}",
                trigger_metadata={"source": "bigpanda", "incident_id": incident_id},
                incident_id=str(aurora_incident_id),
            )
            task = run_background_chat.delay(
                user_id=user_id, session_id=session_id,
                initial_message=_build_rca_prompt(incident, alerts, user_id=user_id),
                trigger_metadata={"source": "bigpanda", "incident_id": incident_id},
                incident_id=str(aurora_incident_id),
            )
            with db_pool.get_admin_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE incidents SET rca_celery_task_id = %s WHERE id = %s",
                    (task.id, str(aurora_incident_id)),
                )
                conn.commit()
            logger.info("[BIGPANDA] Triggered RCA for session %s (task=%s)", session_id, task.id)
        except Exception as chat_exc:
            logger.exception("[BIGPANDA] Failed to trigger background chat: %s", chat_exc)

    except Exception as exc:
        logger.exception("[BIGPANDA] Failed to process webhook for user %s", user_id)
        raise self.retry(exc=exc)
