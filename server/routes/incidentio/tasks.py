"""Celery tasks for incident.io webhook event processing."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from celery_config import celery_app
from chat.background.rca_prompt_builder import build_incidentio_rca_prompt
from services.correlation.alert_correlator import AlertCorrelator
from services.correlation import handle_correlated_alert

logger = logging.getLogger(__name__)


def _should_trigger_rca(user_id: str) -> bool:
    from utils.auth.stateless_auth import get_user_preference
    return get_user_preference(user_id, "incidentio_rca_enabled", default=False)


def _should_postback(user_id: str) -> bool:
    from utils.auth.stateless_auth import get_user_preference
    return get_user_preference(user_id, "incidentio_postback_enabled", default=False)


def _extract_incident_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Extract normalized incident fields from the webhook event envelope."""
    event = payload.get("event", {}) or {}
    incident = event.get("incident") or payload.get("incident") or {}

    severity_obj = incident.get("severity") or {}
    incident_type_obj = incident.get("incident_type") or {}

    return {
        "incident_id": incident.get("id") or payload.get("id"),
        "incident_name": incident.get("name") or incident.get("title") or "Untitled Incident",
        "incident_status": incident.get("status") or event.get("status") or "unknown",
        "severity": severity_obj.get("name", "unknown") if isinstance(severity_obj, dict) else str(severity_obj or "unknown"),
        "incident_type": incident_type_obj.get("name") if isinstance(incident_type_obj, dict) else str(incident_type_obj or ""),
        "summary": incident.get("summary") or "",
        "created_at": incident.get("created_at"),
        "updated_at": incident.get("updated_at"),
        "permalink": incident.get("permalink") or "",
        "custom_fields": incident.get("custom_field_entries") or [],
        "roles": incident.get("incident_role_assignments") or [],
    }


def _map_severity(severity_name: str) -> str:
    """Normalize incident.io severity names to standard levels."""
    s = severity_name.lower().strip()
    if s in ("critical", "sev0", "sev1", "p0", "p1"):
        return "critical"
    if s in ("high", "major", "sev2", "p2"):
        return "high"
    if s in ("medium", "moderate", "sev3", "p3"):
        return "medium"
    if s in ("low", "minor", "sev4", "sev5", "p4", "p5"):
        return "low"
    return "unknown"


@celery_app.task(
    bind=True, max_retries=3, default_retry_delay=30, name="incidentio.process_event"
)
def process_incidentio_event(
    self,
    payload: Dict[str, Any],
    metadata: Optional[Dict[str, Any]] = None,
    user_id: Optional[str] = None,
) -> None:
    """Process an incident.io webhook event."""
    try:
        event_type = payload.get("event_type") or (payload.get("event", {}) or {}).get("type", "unknown")
        fields = _extract_incident_fields(payload)
        logger.info(
            "[INCIDENTIO][EVENT][USER:%s] type=%s incident=%s status=%s severity=%s",
            user_id or "unknown", event_type, fields["incident_name"],
            fields["incident_status"], fields["severity"],
        )

        if not user_id:
            logger.warning("[INCIDENTIO] No user_id — event not stored")
            return

        from utils.db.connection_pool import db_pool

        try:
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cursor:
                    from utils.auth.stateless_auth import set_rls_context
                    org_id = set_rls_context(cursor, conn, user_id, log_prefix="[INCIDENTIO]")
                    if not org_id:
                        return

                    received_at = datetime.now(timezone.utc)
                    normalized_severity = _map_severity(fields["severity"])

                    cursor.execute(
                        """
                        INSERT INTO incidentio_alerts
                        (user_id, org_id, incident_id, incident_name, incident_status,
                         severity, incident_type, payload, received_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (org_id, incident_id) DO UPDATE
                        SET incident_name = EXCLUDED.incident_name,
                            incident_status = EXCLUDED.incident_status,
                            severity = EXCLUDED.severity,
                            incident_type = EXCLUDED.incident_type,
                            payload = EXCLUDED.payload,
                            received_at = EXCLUDED.received_at
                        RETURNING id
                        """,
                        (
                            user_id, org_id, fields["incident_id"],
                            fields["incident_name"], fields["incident_status"],
                            normalized_severity, fields["incident_type"],
                            json.dumps(payload), received_at,
                        ),
                    )
                    alert_row = cursor.fetchone()
                    alert_db_id = alert_row[0] if alert_row else None

                    if not alert_db_id:
                        conn.rollback()
                        logger.error("[INCIDENTIO] Failed to store event for user %s", user_id)
                        return

                    # Only trigger RCA on new incidents, not updates
                    is_new_incident = event_type in (
                        "incident.created", "v2.incidents.created",
                        "incident.declared", "public_incident.incident_created",
                    )

                    if not is_new_incident:
                        conn.commit()
                        logger.info("[INCIDENTIO] Stored update event (no RCA trigger)")
                        return

                    service = _extract_service(fields)
                    alert_metadata = {
                        "permalink": fields["permalink"],
                        "summary": fields["summary"],
                        "event_type": event_type,
                    }
                    if fields["roles"]:
                        alert_metadata["roles"] = [
                            {"role": r.get("role", {}).get("name", ""),
                             "assignee": r.get("assignee", {}).get("name", "")}
                            for r in fields["roles"][:5]
                        ]

                    # Alert correlation
                    try:
                        correlator = AlertCorrelator()
                        correlation_result = correlator.correlate(
                            cursor=cursor,
                            user_id=user_id,
                            source_type="incidentio",
                            source_alert_id=alert_db_id,
                            alert_title=fields["incident_name"],
                            alert_service=service,
                            alert_severity=normalized_severity,
                            alert_metadata=alert_metadata,
                            org_id=org_id,
                        )
                        if correlation_result.is_correlated:
                            handle_correlated_alert(
                                cursor=cursor,
                                user_id=user_id,
                                incident_id=correlation_result.incident_id,
                                source_type="incidentio",
                                source_alert_id=alert_db_id,
                                alert_title=fields["incident_name"],
                                alert_service=service,
                                alert_severity=normalized_severity,
                                correlation_result=correlation_result,
                                alert_metadata=alert_metadata,
                                raw_payload=payload,
                                org_id=org_id,
                            )
                            conn.commit()
                            return
                    except Exception as corr_exc:
                        logger.warning("[INCIDENTIO] Correlation failed, continuing: %s", corr_exc)

                    if not _should_trigger_rca(user_id):
                        conn.commit()
                        logger.info("[INCIDENTIO] Stored incident (RCA disabled)")
                        return

                    # Create Aurora incident record
                    cursor.execute(
                        """
                        INSERT INTO incidents
                        (user_id, org_id, source_type, source_alert_id, alert_title,
                         alert_service, severity, status, started_at, alert_metadata)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (org_id, source_type, source_alert_id, user_id) DO UPDATE
                        SET updated_at = CURRENT_TIMESTAMP,
                            alert_metadata = EXCLUDED.alert_metadata
                        RETURNING id
                        """,
                        (
                            user_id, org_id, "incidentio", alert_db_id,
                            fields["incident_name"], service, normalized_severity,
                            "investigating", received_at, json.dumps(alert_metadata),
                        ),
                    )
                    incident_row = cursor.fetchone()
                    incident_id = incident_row[0] if incident_row else None
                    conn.commit()

                    if not incident_id:
                        return

                    # Link alert to incident
                    try:
                        cursor.execute(
                            """INSERT INTO incident_alerts
                               (user_id, org_id, incident_id, source_type, source_alert_id,
                                alert_title, alert_service, alert_severity, correlation_strategy,
                                correlation_score, alert_metadata)
                               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                            (
                                user_id, org_id, incident_id, "incidentio", alert_db_id,
                                fields["incident_name"], service, normalized_severity,
                                "primary", 1.0, json.dumps(alert_metadata),
                            ),
                        )
                        cursor.execute(
                            "UPDATE incidents SET affected_services = ARRAY[%s] WHERE id = %s",
                            (service, incident_id),
                        )
                        conn.commit()
                    except Exception as e:
                        logger.warning("[INCIDENTIO] Failed to link alert: %s", e)

                # Trigger summary + RCA outside cursor context
                if incident_id:
                    _trigger_rca_pipeline(
                        user_id=user_id,
                        incident_id=incident_id,
                        fields=fields,
                        payload=payload,
                        alert_metadata=alert_metadata,
                        service=service,
                        severity=normalized_severity,
                    )

        except Exception as db_exc:
            logger.exception("[INCIDENTIO] Database error: %s", db_exc)

    except Exception as exc:
        logger.exception("[INCIDENTIO] Failed to process event")
        raise self.retry(exc=exc)


def _extract_service(fields: Dict[str, Any]) -> str:
    """Best-effort service extraction from incident fields."""
    for cf in fields.get("custom_fields") or []:
        field_def = cf.get("custom_field", {})
        if field_def.get("name", "").lower() in ("service", "affected_service", "component"):
            values = cf.get("values") or []
            if values:
                return str(values[0].get("label") or values[0].get("value", ""))[:255]

    name = fields.get("incident_name", "")
    if ":" in name:
        return name.split(":")[0].strip()[:255]

    return fields.get("incident_type") or "unknown"


def _trigger_rca_pipeline(
    user_id: str,
    incident_id: int,
    fields: Dict[str, Any],
    payload: Dict[str, Any],
    alert_metadata: Dict[str, Any],
    service: str,
    severity: str,
) -> None:
    """Trigger summary generation and background RCA for an incident."""
    from chat.background.summarization import generate_incident_summary

    generate_incident_summary.delay(
        incident_id=str(incident_id),
        user_id=user_id,
        source_type="incidentio",
        alert_title=fields["incident_name"],
        severity=severity,
        service=service,
        raw_payload=payload,
        alert_metadata=alert_metadata,
    )

    try:
        from chat.background.task import (
            run_background_chat,
            create_background_chat_session,
            is_background_chat_allowed,
        )

        if not is_background_chat_allowed(user_id):
            logger.info("[INCIDENTIO] Background RCA rate-limited for user %s", user_id)
            return

        chat_title = f"RCA: {fields['incident_name']}"
        session_id = create_background_chat_session(
            user_id=user_id,
            title=chat_title,
            trigger_metadata={
                "source": "incidentio",
                "incident_id": fields["incident_id"],
                "incident_name": fields["incident_name"],
                "permalink": fields["permalink"],
            },
            incident_id=str(incident_id),
        )

        rca_prompt = build_incidentio_rca_prompt(payload, user_id=user_id)

        task = run_background_chat.delay(
            user_id=user_id,
            session_id=session_id,
            initial_message=rca_prompt,
            trigger_metadata={
                "source": "incidentio",
                "incident_id": fields["incident_id"],
                "incident_name": fields["incident_name"],
            },
            incident_id=str(incident_id),
        )

        # Store task ID for cancellation support
        from utils.db.connection_pool import db_pool
        try:
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "UPDATE incidents SET rca_celery_task_id = %s WHERE id = %s",
                        (task.id, str(incident_id)),
                    )
                    conn.commit()
        except Exception:
            pass

        logger.info("[INCIDENTIO] Triggered RCA for incident %s (task=%s)", incident_id, task.id)

        # Post-back RCA summary if enabled
        if _should_postback(user_id):
            postback_rca_to_incidentio.delay(user_id, str(incident_id), fields["incident_id"])

    except Exception as exc:
        logger.exception("[INCIDENTIO] Failed to trigger RCA: %s", exc)


@celery_app.task(
    bind=True, max_retries=2, default_retry_delay=120, name="incidentio.postback_rca"
)
def postback_rca_to_incidentio(
    self,
    user_id: str,
    aurora_incident_id: str,
    incidentio_incident_id: str,
) -> None:
    """Post RCA results back to incident.io timeline once analysis completes."""
    try:
        from utils.db.connection_pool import db_pool
        from utils.auth.token_management import get_token_data
        from routes.incidentio.incidentio_routes import IncidentioClient

        # Wait for RCA to complete — check for summary in incidents table
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT summary, status FROM incidents WHERE id = %s",
                    (aurora_incident_id,),
                )
                row = cursor.fetchone()

        if not row or not row[0]:
            if self.request.retries < self.max_retries:
                raise self.retry(countdown=120)
            logger.info("[INCIDENTIO] No RCA summary after retries, skipping postback")
            return

        summary, status = row
        if status not in ("analyzed", "completed"):
            if self.request.retries < self.max_retries:
                raise self.retry(countdown=120)
            return

        creds = get_token_data(user_id, "incidentio")
        if not creds or not creds.get("api_key"):
            logger.warning("[INCIDENTIO] No credentials for postback")
            return

        client = IncidentioClient(creds["api_key"])
        message = f"🔍 **Aurora RCA Summary**\n\n{summary}"
        client.post_incident_update(incidentio_incident_id, message)
        logger.info("[INCIDENTIO] Posted RCA back to incident %s", incidentio_incident_id)

    except Exception as exc:
        if "retry" not in str(type(exc).__name__).lower():
            logger.exception("[INCIDENTIO] Postback failed: %s", exc)
            raise self.retry(exc=exc)
        raise
