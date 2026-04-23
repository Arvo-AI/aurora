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
    return get_user_preference(user_id, "incidentio_rca_enabled", default=True)


def _should_postback(user_id: str) -> bool:
    from utils.auth.stateless_auth import get_user_preference
    return get_user_preference(user_id, "incidentio_postback_enabled", default=False)


def _resolve_incident_object(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Find the incident dict inside an incident.io webhook payload."""
    event = payload.get("event", {}) or {}
    incident = event.get("incident") or payload.get("incident") or None

    if not incident:
        for key, value in payload.items():
            if key.startswith("public_incident.") and isinstance(value, dict):
                incident = value.get("incident") or value
                break

    return incident or {}


def _safe_name(obj, default: str = "") -> str:
    """Extract .name from a dict-or-scalar field."""
    if isinstance(obj, dict):
        return obj.get("name", default)
    return str(obj) if obj else default


def _extract_incident_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Extract normalized incident fields from the webhook event envelope."""
    event = payload.get("event", {}) or {}
    incident = _resolve_incident_object(payload)

    return {
        "incident_id": incident.get("id") or payload.get("id"),
        "incident_name": incident.get("name") or incident.get("title") or "Untitled Incident",
        "incident_status": incident.get("status") or event.get("status") or "unknown",
        "severity": _safe_name(incident.get("severity"), "unknown"),
        "incident_type": _safe_name(incident.get("incident_type")),
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


_NEW_INCIDENT_EVENTS = frozenset((
    "incident.created", "v2.incidents.created",
    "incident.declared", "public_incident.incident_created",
    "public_incident.incident_created_v2",
))


def _build_alert_metadata(fields: Dict[str, Any], event_type: str) -> Dict[str, Any]:
    meta: Dict[str, Any] = {
        "permalink": fields["permalink"],
        "summary": fields["summary"],
        "event_type": event_type,
    }
    if fields["roles"]:
        meta["roles"] = [
            {"role": r.get("role", {}).get("name", ""),
             "assignee": r.get("assignee", {}).get("name", "")}
            for r in fields["roles"][:5]
        ]
    return meta


def _try_correlate(cursor, conn, *, user_id, alert_db_id, fields, service,
                   normalized_severity, alert_metadata, payload, org_id) -> bool:
    """Attempt alert correlation. Returns True if correlated (and committed)."""
    try:
        cursor.execute("SAVEPOINT sp_correlation")
        correlator = AlertCorrelator()
        result = correlator.correlate(
            cursor=cursor, user_id=user_id, source_type="incidentio",
            source_alert_id=alert_db_id, alert_title=fields["incident_name"],
            alert_service=service, alert_severity=normalized_severity,
            alert_metadata=alert_metadata, org_id=org_id,
        )
        if result.is_correlated:
            handle_correlated_alert(
                cursor=cursor, user_id=user_id, incident_id=result.incident_id,
                source_type="incidentio", source_alert_id=alert_db_id,
                alert_title=fields["incident_name"], alert_service=service,
                alert_severity=normalized_severity, correlation_result=result,
                alert_metadata=alert_metadata, raw_payload=payload, org_id=org_id,
            )
            conn.commit()
            return True
        cursor.execute("RELEASE SAVEPOINT sp_correlation")
    except Exception as corr_exc:
        cursor.execute("ROLLBACK TO SAVEPOINT sp_correlation")
        logger.warning("[INCIDENTIO] Correlation failed, continuing: %s", corr_exc)
    return False


def _create_and_link_incident(cursor, conn, *, user_id, org_id, alert_db_id,
                              fields, service, normalized_severity,
                              alert_metadata, received_at) -> Optional[str]:
    """Create Aurora incident record and link the alert. Returns incident_id or None."""
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
    row = cursor.fetchone()
    incident_id = row[0] if row else None
    conn.commit()

    if not incident_id:
        return None

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
        conn.rollback()
        logger.warning("[INCIDENTIO] Failed to link alert: %s", e)

    return str(incident_id)


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

        _store_and_process_event(user_id, event_type, fields, payload)

    except Exception as exc:
        logger.exception("[INCIDENTIO] Failed to process event")
        raise self.retry(exc=exc)


def _store_and_process_event(user_id: str, event_type: str,
                             fields: Dict[str, Any], payload: Dict[str, Any]) -> None:
    """Store the alert and optionally trigger correlation/RCA."""
    from utils.db.connection_pool import db_pool
    from utils.auth.stateless_auth import set_rls_context

    incident_id = None
    service = ""
    normalized_severity = ""
    alert_metadata: Dict[str, Any] = {}

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            org_id = set_rls_context(cursor, conn, user_id, log_prefix="[INCIDENTIO]")
            if not org_id:
                return

            received_at = datetime.now(timezone.utc)
            normalized_severity = _map_severity(fields["severity"])

            alert_db_id = _upsert_alert(cursor, conn, user_id=user_id, org_id=org_id,
                                        fields=fields, payload=payload,
                                        severity=normalized_severity, received_at=received_at)
            if not alert_db_id:
                conn.rollback()
                logger.error("[INCIDENTIO] Failed to store event for user %s", user_id)
                return

            if event_type not in _NEW_INCIDENT_EVENTS:
                conn.commit()
                logger.info("[INCIDENTIO] Stored update event (no RCA trigger)")
                return

            service = _extract_service(fields)
            alert_metadata = _build_alert_metadata(fields, event_type)

            if _try_correlate(cursor, conn, user_id=user_id, alert_db_id=alert_db_id,
                              fields=fields, service=service,
                              normalized_severity=normalized_severity,
                              alert_metadata=alert_metadata, payload=payload, org_id=org_id):
                return

            if not _should_trigger_rca(user_id):
                conn.commit()
                logger.info("[INCIDENTIO] Stored incident (RCA disabled)")
                return

            incident_id = _create_and_link_incident(
                cursor, conn, user_id=user_id, org_id=org_id,
                alert_db_id=alert_db_id, fields=fields, service=service,
                normalized_severity=normalized_severity,
                alert_metadata=alert_metadata, received_at=received_at,
            )

    if incident_id:
        _trigger_rca_pipeline(
            user_id=user_id, incident_id=incident_id, fields=fields,
            payload=payload, alert_metadata=alert_metadata,
            service=service, severity=normalized_severity,
        )


def _upsert_alert(cursor, conn, *, user_id, org_id, fields, payload,
                   severity, received_at) -> Optional[int]:
    """Insert or update the incidentio_alerts row. Returns the DB id or None."""
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
            severity, fields["incident_type"],
            json.dumps(payload), received_at,
        ),
    )
    row = cursor.fetchone()
    return row[0] if row else None


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
    incident_id: str,
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
        except Exception as exc:
            logger.warning("[INCIDENTIO] Failed to store RCA task ID for incident %s: %s", incident_id, exc)

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
                    "SELECT aurora_summary, aurora_status FROM incidents WHERE id = %s",
                    (aurora_incident_id,),
                )
                row = cursor.fetchone()

        if not row or not row[0]:
            if self.request.retries < self.max_retries:
                raise self.retry(countdown=120)
            logger.info("[INCIDENTIO] No RCA summary after retries, skipping postback")
            return

        summary, aurora_status = row
        if aurora_status not in ("analyzed", "completed"):
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
