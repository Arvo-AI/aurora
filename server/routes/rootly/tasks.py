"""Celery tasks for Rootly webhook processing.

Processes Rootly incident webhooks and feeds them into Aurora's
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
    "major": "critical",
    "high": "high",
    "medium": "medium",
    "minor": "medium",
    "low": "low",
    "warning": "low",
}


def _extract_severity(incident: Dict[str, Any]) -> str:
    raw = str(incident.get("severity", "unknown")).lower()
    return _SEVERITY_MAP.get(raw, "medium")


def _extract_title(incident: Dict[str, Any]) -> str:
    return incident.get("title") or incident.get("summary") or f"Rootly Incident {incident.get('id', 'unknown')}"


def _extract_service(incident: Dict[str, Any]) -> str:
    services = incident.get("services") or []
    if services and isinstance(services, list):
        first = services[0]
        if isinstance(first, dict):
            return first.get("name") or first.get("slug") or "unknown"
        return str(first)[:255]
    functionality = incident.get("functionality") or {}
    if isinstance(functionality, dict) and functionality.get("name"):
        return functionality["name"]
    return "unknown"


def _normalize_status(rootly_status: str) -> str:
    status_lower = rootly_status.lower()
    if status_lower in ("resolved", "cancelled"):
        return "resolved"
    if status_lower == "mitigated":
        return "mitigated"
    return "investigating"


def _build_alert_metadata(incident: Dict[str, Any]) -> Dict[str, Any]:
    meta: Dict[str, Any] = {}
    if url := incident.get("url"):
        meta["incidentUrl"] = url
    if incident_id := incident.get("id"):
        meta["incidentId"] = incident_id
    if slug := incident.get("slug"):
        meta["slug"] = slug
    if severity := incident.get("severity"):
        meta["severity"] = severity
    if summary := incident.get("summary"):
        meta["description"] = summary
    if labels := incident.get("labels"):
        meta["labels"] = labels
    if environments := incident.get("environments"):
        env_names = [e.get("name") or str(e) for e in environments if isinstance(e, dict)]
        if env_names:
            meta["environments"] = env_names
    if groups := incident.get("groups"):
        group_names = [g.get("name") or str(g) for g in groups if isinstance(g, dict)]
        if group_names:
            meta["groups"] = group_names
    if types := incident.get("incident_types"):
        type_names = [t.get("name") or str(t) for t in types if isinstance(t, dict)]
        if type_names:
            meta["incidentTypes"] = type_names
    return meta


def _should_trigger_rca(user_id: str) -> bool:
    return get_user_preference(user_id, "rootly_rca_enabled", default=False)


def _build_rca_prompt(incident: Dict[str, Any], user_id: str | None = None) -> str:
    from chat.background.rca_prompt_builder import build_rootly_rca_prompt
    return build_rootly_rca_prompt(incident, user_id=user_id)


@celery_app.task(
    bind=True, max_retries=3, default_retry_delay=30,
    name="rootly.process_event",
)
def process_rootly_event(
    self,
    raw_payload: Dict[str, Any],
    metadata: Optional[Dict[str, Any]] = None,
    user_id: Optional[str] = None,
) -> None:
    """Process a Rootly webhook payload through the full correlation pipeline."""
    received_at = datetime.now(timezone.utc)

    if not user_id:
        logger.warning("[ROOTLY] Received event with no user_id, skipping")
        return

    try:
        from utils.db.connection_pool import db_pool

        event = raw_payload.get("event", {})
        event_type = event.get("type", "")
        incident = raw_payload.get("data", {})

        if isinstance(incident, dict) and "attributes" in incident:
            attrs = incident.get("attributes", {})
            incident_id = incident.get("id") or attrs.get("id")
            incident = {**attrs, "id": incident_id}
        else:
            incident_id = incident.get("id")

        if not incident_id:
            logger.warning("[ROOTLY] Payload missing incident ID, skipping")
            return

        rootly_status = incident.get("status", "started")
        title = _extract_title(incident)
        severity = _extract_severity(incident)
        service = _extract_service(incident)
        alert_metadata = _build_alert_metadata(incident)

        logger.info("[ROOTLY][ALERT][USER:%s] %s", user_id, title)

        with db_pool.get_admin_connection() as conn, conn.cursor() as cursor:

            from utils.auth.stateless_auth import set_rls_context
            org_id = set_rls_context(cursor, conn, user_id, log_prefix="[ROOTLY]")
            if not org_id:
                return

            cursor.execute(
                """INSERT INTO rootly_events
                   (user_id, org_id, event_type, incident_id, incident_title, incident_status,
                    incident_severity, service_name, payload, received_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                (
                    user_id, org_id, event_type, str(incident_id),
                    title[:500] if title else None,
                    rootly_status, incident.get("severity"),
                    service[:255] if service else None,
                    json.dumps(raw_payload), received_at,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                logger.error("[ROOTLY] INSERT returned no row for user %s, incident %s", user_id, incident_id)
                return
            alert_db_id = row[0]
            conn.commit()

            if event_type == "incident.created":
                try:
                    correlator = AlertCorrelator()
                    result = correlator.correlate(
                        cursor=cursor, user_id=user_id, source_type="rootly",
                        source_alert_id=alert_db_id, alert_title=title,
                        alert_service=service, alert_severity=severity,
                        alert_metadata=alert_metadata, org_id=org_id,
                    )
                    if result.is_correlated:
                        handle_correlated_alert(
                            cursor=cursor, user_id=user_id, incident_id=result.incident_id,
                            source_type="rootly", source_alert_id=alert_db_id,
                            alert_title=title, alert_service=service, alert_severity=severity,
                            correlation_result=result, alert_metadata=alert_metadata,
                            raw_payload=raw_payload, org_id=org_id,
                        )
                        conn.commit()
                        return
                except Exception as corr_exc:
                    logger.warning("[ROOTLY] Correlation failed, proceeding: %s", corr_exc)

            if not _should_trigger_rca(user_id):
                conn.commit()
                logger.info("[ROOTLY] Stored for user %s (RCA disabled)", user_id)
                return

            aurora_status = _normalize_status(rootly_status)

            cursor.execute(
                """INSERT INTO incidents
                   (user_id, org_id, source_type, source_alert_id, alert_title, alert_service,
                    severity, status, started_at, alert_metadata)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (org_id, source_type, source_alert_id, user_id) DO UPDATE
                   SET updated_at = CURRENT_TIMESTAMP,
                       status = EXCLUDED.status,
                       severity = EXCLUDED.severity,
                       started_at = CASE WHEN incidents.status = 'resolved' AND EXCLUDED.status != 'resolved'
                                    THEN EXCLUDED.started_at ELSE incidents.started_at END,
                       alert_metadata = EXCLUDED.alert_metadata
                   RETURNING id""",
                (user_id, org_id, "rootly", alert_db_id, title, service,
                 severity, aurora_status, received_at, json.dumps(alert_metadata)),
            )
            incident_row = cursor.fetchone()
            aurora_incident_id = incident_row[0] if incident_row else None

            if aurora_incident_id and event_type == "incident.created":
                cursor.execute(
                    """INSERT INTO incident_alerts
                       (user_id, org_id, incident_id, source_type, source_alert_id, alert_title,
                        alert_service, alert_severity, correlation_strategy, correlation_score, alert_metadata)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (user_id, org_id, aurora_incident_id, "rootly", alert_db_id, title,
                     service, severity, "primary", 1.0, json.dumps(alert_metadata)),
                )
                cursor.execute(
                    "UPDATE incidents SET affected_services = ARRAY[%s] WHERE id = %s",
                    (service, aurora_incident_id),
                )
            conn.commit()

        if not aurora_incident_id:
            return

        logger.info("[ROOTLY] Created incident %s for event %s", aurora_incident_id, alert_db_id)

        try:
            from routes.incidents_sse import broadcast_incident_update_to_user_connections
            broadcast_incident_update_to_user_connections(
                user_id,
                {"type": "incident_update", "incident_id": str(aurora_incident_id), "source": "rootly"},
            )
        except Exception as e:
            logger.warning("[ROOTLY] Failed to notify SSE: %s", e)

        if event_type == "incident.created":
            from chat.background.summarization import generate_incident_summary
            generate_incident_summary.delay(
                incident_id=str(aurora_incident_id), user_id=user_id, source_type="rootly",
                alert_title=title, severity=severity, service=service,
                raw_payload=raw_payload, alert_metadata=alert_metadata,
            )

            try:
                from chat.background.task import (
                    run_background_chat, create_background_chat_session, is_background_chat_allowed,
                )
                if not is_background_chat_allowed(user_id):
                    logger.info("[ROOTLY] Skipping RCA - rate limited for user %s", user_id)
                    return

                session_id = create_background_chat_session(
                    user_id=user_id,
                    title=f"RCA: {title}",
                    trigger_metadata={"source": "rootly", "incident_id": str(incident_id)},
                    incident_id=str(aurora_incident_id),
                )
                task = run_background_chat.delay(
                    user_id=user_id, session_id=session_id,
                    initial_message=_build_rca_prompt(incident, user_id=user_id),
                    trigger_metadata={"source": "rootly", "incident_id": str(incident_id)},
                    incident_id=str(aurora_incident_id),
                )
                with db_pool.get_admin_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE incidents SET rca_celery_task_id = %s WHERE id = %s",
                        (task.id, str(aurora_incident_id)),
                    )
                    conn.commit()
                logger.info("[ROOTLY] Triggered RCA for session %s (task=%s)", session_id, task.id)
            except Exception as chat_exc:
                logger.exception("[ROOTLY] Failed to trigger background chat: %s", chat_exc)

    except Exception as exc:
        logger.exception("[ROOTLY] Failed to process webhook for user %s", user_id)
        raise self.retry(exc=exc)
