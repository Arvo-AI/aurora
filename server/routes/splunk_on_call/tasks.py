"""Celery processing for Splunk On-Call incidents."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from celery_config import celery_app
from chat.background.rca_prompt_builder import build_rca_prompt
from utils.auth.stateless_auth import get_user_preference, set_rls_context
from utils.auth.token_management import get_token_data
from utils.payload_timestamp import extract_alert_fired_at

logger = logging.getLogger(__name__)


def _value(data: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        value = data.get(name)
        if value is not None and value != "":
            return value
    return default


def _section_value(
    data: dict[str, Any],
    section: str,
    *names: str,
    default: Any = None,
) -> Any:
    nested = data.get(section)
    if isinstance(nested, dict):
        value = _value(nested, *names)
        if value is not None:
            return value
    dotted_names = tuple(f"{section}.{name}" for name in names)
    return _value(data, *dotted_names, default=default)


def _incidents(payload: dict[str, Any]) -> list[dict[str, Any]]:
    values = payload.get("incidents")
    if isinstance(values, list):
        return [item for item in values if isinstance(item, dict)]
    incident = payload.get("incident")
    if isinstance(incident, dict):
        return [incident]
    return [payload]


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    phase = str(
        _value(raw, "currentPhase", "current_phase")
        or _section_value(raw, "INCIDENT", "CURRENT_PHASE")
        or _section_value(raw, "STATE", "CURRENT_ALERT_PHASE")
        or _value(raw, "state")
        or "UNACKED"
    ).upper()
    incident_number = str(
        _value(raw, "incidentNumber", "incident_number", "incident_id", "id")
        or _section_value(raw, "INCIDENT", "INCIDENT_ID")
        or ""
    )
    title = str(
        _value(
            raw,
            "entityDisplayName",
            "entity_display_name",
            "stateMessage",
            "state_message",
            "incident_name",
        )
        or _section_value(raw, "STATE", "INCIDENT_NAME")
        or _section_value(raw, "INCIDENT", "INCIDENT_NAME", "SERVICE")
        or _section_value(raw, "ALERT", "state_message", "entity_display_name")
        or _value(raw, "entityId", "entity_id")
        or f"Splunk On-Call incident {incident_number or 'unknown'}"
    )
    return {
        "incident_number": incident_number,
        "phase": phase,
        "title": title[:500],
        "service": str(
            _value(raw, "service", "monitoring_tool")
            or _section_value(raw, "INCIDENT", "SERVICE")
            or _section_value(raw, "ALERT", "monitoring_tool")
            or _value(raw, "host")
            or "unknown"
        )[:255],
        "routing_key": str(
            _value(raw, "routingKey", "routing_key")
            or _section_value(raw, "ALERT", "routing_key")
            or ""
        ),
        "host": str(
            _value(raw, "host")
            or _section_value(raw, "STATE", "HOST")
            or ""
        ),
        "entity_id": str(
            _value(raw, "entityId", "entity_id")
            or _section_value(raw, "ALERT", "entity_id")
            or ""
        ),
        "entity_state": str(
            _value(raw, "entityState", "entity_state")
            or _section_value(raw, "INCIDENT", "ENTITY_STATE")
            or _section_value(raw, "ALERT", "entity_state", "SERVICESTATE")
            or ""
        ),
        "started_at": (
            _value(raw, "startTime", "start_time", "started_at")
            or _section_value(raw, "STATE", "INCIDENT_TIMESTAMP")
        ),
        "last_alert_time": _value(raw, "lastAlertTime", "last_alert_time"),
        "last_alert_id": str(
            _value(raw, "lastAlertId", "last_alert_id", default="")
        ),
        "incident_url": _value(
            raw, "incidentLink", "incident_link", "incident_url"
        ),
    }


def _severity(incident: dict[str, Any]) -> str:
    state = incident["entity_state"].lower()
    if "critical" in state:
        return "critical"
    if "warning" in state:
        return "high"
    return "medium"


def _should_trigger_rca(user_id: str) -> bool:
    return get_user_preference(
        user_id, "splunk_on_call_rca_enabled", default=True
    )


def _store_and_create(raw: dict[str, Any], user_id: str) -> None:
    from utils.db.connection_pool import db_pool

    incident = _normalize(raw)
    if not incident["incident_number"]:
        logger.warning("[SPLUNK_ON_CALL] Skipping payload without incident number")
        return

    creds = get_token_data(user_id, "splunk_on_call") or {}
    routing_filter = str(creds.get("routing_key_contains") or "").lower()
    if routing_filter and routing_filter not in incident["routing_key"].lower():
        logger.info(
            "[SPLUNK_ON_CALL] Skipping incident %s: routing key does not match filter",
            incident["incident_number"],
        )
        return

    received_at = datetime.now(timezone.utc)
    severity = _severity(incident)
    aurora_status = (
        "resolved" if incident["phase"] == "RESOLVED" else "investigating"
    )
    metadata = {
        key: value
        for key, value in {
            "splunkOnCallIncidentNumber": incident["incident_number"],
            "routingKey": incident["routing_key"],
            "host": incident["host"],
            "entityId": incident["entity_id"],
            "entityState": incident["entity_state"],
            "lastAlertId": incident["last_alert_id"],
            "incidentUrl": incident["incident_url"],
        }.items()
        if value
    }

    with db_pool.get_admin_connection() as conn, conn.cursor() as cursor:
        org_id = set_rls_context(
            cursor, conn, user_id, log_prefix="[SPLUNK_ON_CALL]"
        )
        if not org_id:
            return

        cursor.execute(
            """
            INSERT INTO splunk_on_call_events
                (user_id, org_id, incident_number, incident_phase, incident_title,
                 routing_key, service, payload, received_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (org_id, user_id, incident_number) DO UPDATE
            SET incident_phase = EXCLUDED.incident_phase,
                incident_title = EXCLUDED.incident_title,
                routing_key = EXCLUDED.routing_key,
                service = EXCLUDED.service,
                payload = EXCLUDED.payload,
                received_at = EXCLUDED.received_at
            RETURNING id, (xmax = 0) AS inserted
            """,
            (
                user_id,
                org_id,
                incident["incident_number"],
                incident["phase"],
                incident["title"],
                incident["routing_key"],
                incident["service"],
                json.dumps(raw),
                received_at,
            ),
        )
        event_id, is_new_event = cursor.fetchone()

        cursor.execute(
            """
            SELECT id FROM incidents
            WHERE org_id = %s AND user_id = %s
              AND source_type = 'splunk_on_call' AND source_alert_id = %s
            """,
            (org_id, user_id, event_id),
        )
        existing = cursor.fetchone()
        if existing:
            cursor.execute(
                """
                UPDATE incidents
                SET status = %s, updated_at = CURRENT_TIMESTAMP,
                    alert_title = %s, alert_service = %s,
                    severity = %s, alert_metadata = %s
                WHERE id = %s
                """,
                (
                    aurora_status,
                    incident["title"],
                    incident["service"],
                    severity,
                    json.dumps(metadata),
                    existing[0],
                ),
            )
            conn.commit()
            logger.info(
                "[SPLUNK_ON_CALL] Updated incident %s to %s",
                incident["incident_number"],
                incident["phase"],
            )
            return

        if incident["phase"] not in ("UNACKED", "TRIGGERED") or not is_new_event:
            conn.commit()
            return

        alert_fired_at = extract_alert_fired_at(
            raw,
            [
                "startTime",
                "start_time",
                "lastAlertTime",
                "last_alert_time",
                "STATE.INCIDENT_TIMESTAMP",
                "ALERT.VO_ALERT_RCV_TIME",
            ],
        )
        cursor.execute(
            """
            INSERT INTO incidents
                (user_id, org_id, source_type, source_alert_id, alert_title,
                 alert_service, severity, status, started_at, alert_metadata,
                 alert_fired_at)
            VALUES (%s, %s, 'splunk_on_call', %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                user_id,
                org_id,
                event_id,
                incident["title"],
                incident["service"],
                severity,
                aurora_status,
                received_at,
                json.dumps(metadata),
                alert_fired_at,
            ),
        )
        row = cursor.fetchone()
        aurora_incident_id = row[0] if row else None
        if aurora_incident_id:
            cursor.execute(
                """
                INSERT INTO incident_alerts
                    (user_id, org_id, incident_id, source_type, source_alert_id,
                     alert_title, alert_service, alert_severity,
                     correlation_strategy, correlation_score, alert_metadata)
                VALUES (%s, %s, %s, 'splunk_on_call', %s, %s, %s, %s, 'primary', 1.0, %s)
                """,
                (
                    user_id,
                    org_id,
                    aurora_incident_id,
                    event_id,
                    incident["title"],
                    incident["service"],
                    severity,
                    json.dumps(metadata),
                ),
            )
            cursor.execute(
                "UPDATE incidents SET affected_services = ARRAY[%s] WHERE id = %s",
                (incident["service"], aurora_incident_id),
            )
        conn.commit()

    if not aurora_incident_id or not _should_trigger_rca(user_id):
        return

    from chat.background.summarization import generate_incident_summary

    generate_incident_summary.delay(
        incident_id=str(aurora_incident_id),
        user_id=user_id,
        source_type="splunk_on_call",
        alert_title=incident["title"],
        severity=severity,
        service=incident["service"],
        raw_payload=raw,
        alert_metadata=metadata,
    )

    try:
        from chat.background.task import (
            create_background_chat_session,
            is_background_chat_allowed,
            run_background_chat,
        )

        if not is_background_chat_allowed(user_id):
            return
        session_id = create_background_chat_session(
            user_id=user_id,
            title=f"RCA: {incident['title']}",
            trigger_metadata={
                "source": "splunk_on_call",
                "incident_number": incident["incident_number"],
            },
            incident_id=str(aurora_incident_id),
        )
        prompt, rail_text = build_rca_prompt(
            "splunk_on_call", incident["title"], raw, user_id=user_id
        )
        task = run_background_chat.delay(
            user_id=user_id,
            session_id=session_id,
            initial_message=prompt,
            trigger_metadata={
                "source": "splunk_on_call",
                "incident_number": incident["incident_number"],
            },
            incident_id=str(aurora_incident_id),
            rail_text=rail_text,
        )
        with db_pool.get_admin_connection() as conn, conn.cursor() as cursor:
            if set_rls_context(
                cursor, conn, user_id, log_prefix="[SPLUNK_ON_CALL:task]"
            ):
                cursor.execute(
                    "UPDATE incidents SET rca_celery_task_id = %s WHERE id = %s",
                    (task.id, str(aurora_incident_id)),
                )
                conn.commit()
    except Exception:
        logger.exception("[SPLUNK_ON_CALL] Failed to trigger RCA")


@celery_app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    name="splunk_on_call.process_event",
)
def process_splunk_on_call_event(
    self,
    payload: dict[str, Any],
    user_id: str,
) -> None:
    try:
        for incident in _incidents(payload):
            _store_and_create(incident, user_id)
    except Exception as exc:
        logger.exception("[SPLUNK_ON_CALL] Failed to process webhook")
        raise self.retry(exc=exc)
