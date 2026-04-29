"""Celery tasks for New Relic webhook processing."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from celery_config import celery_app
from chat.background.rca_prompt_builder import build_newrelic_rca_prompt
from services.correlation.alert_correlator import AlertCorrelator
from services.correlation import handle_correlated_alert
from utils.payload_timestamp import extract_alert_fired_at

logger = logging.getLogger(__name__)


def extract_newrelic_title(payload: Dict[str, Any], default: str = "New Relic Alert") -> str:
    """Extract alert title from a New Relic webhook payload.

    New Relic uses different payload shapes depending on webhook type:
    - Workflow notifications use ``title`` or ``issueTitle``
    - Issue payloads use ``issueTitle`` or ``issue_title``
    - Nested annotations use ``annotations.title``
    - Classic/legacy payloads use ``conditionName`` or ``condition_name``
    - Some payloads only have ``policy_name`` + ``condition_name``
    """
    # Direct top-level title fields
    title = (
        payload.get("title")
        or payload.get("issueTitle")
        or payload.get("issue_title")
    )
    if title:
        return str(title)

    # annotations.title (New Relic workflow variable)
    annotations = payload.get("annotations")
    if isinstance(annotations, dict):
        ann_title = annotations.get("title")
        if ann_title:
            if isinstance(ann_title, list):
                ann_title = ann_title[0] if ann_title else None
            if ann_title:
                return str(ann_title)

    # Condition name (camelCase or snake_case)
    condition_name = payload.get("conditionName") or payload.get("condition_name")
    if condition_name:
        return str(condition_name)

    # Build from policy + entity as last resort
    targets = payload.get("targets") or []
    if targets and isinstance(targets, list):
        target_name = targets[0].get("name") if targets[0] else None
        if target_name:
            return str(target_name)

    entity_names = payload.get("entitiesData", {}).get("names")
    if isinstance(entity_names, list) and entity_names:
        return str(entity_names[0])
    entities = payload.get("entitiesData", {}).get("entities", [])
    if entities and isinstance(entities, list):
        name = entities[0].get("name")
        if name:
            return str(name)

    return default


def _summarize_event(payload: Dict[str, Any]) -> str:
    title = extract_newrelic_title(payload)
    state = payload.get("state") or payload.get("currentState") or payload.get("current_state") or payload.get("status")
    priority = payload.get("priority") or payload.get("severity")
    issue_id = payload.get("issueId") or payload.get("issue_id") or payload.get("incidentId") or payload.get("incident_id")
    parts = [title]
    if state:
        parts.append(f"[{state}]")
    if priority:
        parts.append(f"priority={priority}")
    if issue_id:
        parts.append(f"issue={issue_id}")
    return " ".join(parts)


def _extract_severity(payload: Dict[str, Any]) -> str:
    """Map New Relic priority/severity to Aurora severity levels."""
    priority = (
        payload.get("priority") or payload.get("severity") or ""
    ).upper()

    if priority == "CRITICAL":
        return "critical"
    elif priority in ("HIGH", "WARNING"):
        return "high"
    elif priority in ("MEDIUM", "LOW"):
        return "medium"

    state = (payload.get("state") or payload.get("currentState") or payload.get("current_state") or "").upper()
    if state == "CRITICAL":
        return "critical"
    if state in ("WARNING", "ACTIVATED"):
        return "high"

    return "unknown"


def _extract_service(payload: Dict[str, Any]) -> str:
    """Extract the affected service/entity name from the New Relic payload."""
    # New Relic webhook payloads nest entity info in various locations
    entities: List[Dict[str, Any]] = payload.get("entitiesData", {}).get("entities", [])
    if entities and isinstance(entities, list):
        first = entities[0] if entities else {}
        name = first.get("name") or first.get("entityName")
        if name:
            return str(name)[:255]

    # entitiesData.names (flat set from workflow variables)
    entity_names = payload.get("entitiesData", {}).get("names")
    if isinstance(entity_names, list) and entity_names:
        return str(entity_names[0])[:255]

    # Targets (common in legacy/classic payloads)
    targets = payload.get("targets")
    if targets and isinstance(targets, list) and targets[0].get("name"):
        return str(targets[0]["name"])[:255]

    entity_name = payload.get("entityName") or payload.get("entity_name")
    if entity_name:
        return str(entity_name)[:255]

    # Condition name as last resort (may indicate the service)
    condition_name = payload.get("conditionName") or payload.get("condition_name")
    if condition_name:
        return str(condition_name)[:255]

    return "unknown"


def _build_alert_metadata(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Extract New Relic-specific fields for alert metadata."""
    meta: Dict[str, Any] = {}

    for key in ("issueId", "issue_id", "incidentId", "incident_id"):
        val = payload.get(key)
        if val:
            meta["issueId"] = str(val)
            break

    if payload.get("issueUrl") or payload.get("violationChartUrl") or payload.get("incident_url"):
        meta["alertUrl"] = payload.get("issueUrl") or payload.get("violationChartUrl") or payload.get("incident_url")

    if payload.get("conditionName") or payload.get("condition_name"):
        meta["conditionName"] = payload.get("conditionName") or payload.get("condition_name")

    if payload.get("policyName") or payload.get("policy_name"):
        meta["policyName"] = payload.get("policyName") or payload.get("policy_name")

    if payload.get("accountId") or payload.get("account_id"):
        meta["accountId"] = str(payload.get("accountId") or payload.get("account_id"))

    entities = payload.get("entitiesData", {}).get("entities", [])
    if entities:
        meta["entities"] = [
            {"name": e.get("name"), "type": e.get("type"), "id": e.get("id")}
            for e in entities[:10]
        ]

    if payload.get("totalIncidents"):
        meta["totalIncidents"] = payload["totalIncidents"]

    targets = payload.get("targets", [])
    if targets:
        meta["targets"] = [
            {"name": t.get("name"), "type": t.get("type"), "product": t.get("product")}
            for t in targets[:10]
        ]

    return meta


@celery_app.task(
    bind=True, max_retries=3, default_retry_delay=30, name="newrelic.process_event"
)
def process_newrelic_event(
    self,
    payload: Dict[str, Any],
    metadata: Optional[Dict[str, Any]] = None,
    user_id: Optional[str] = None,
) -> None:
    """Background processor for New Relic webhook payloads."""
    summary = _summarize_event(payload)
    logger.info("[NEWRELIC][WEBHOOK][USER:%s] %s", user_id or "unknown", summary)

    try:
        if not user_id:
            logger.warning("[NEWRELIC][WEBHOOK] Missing user_id; skipping")
            return

        from utils.db.connection_pool import db_pool

        event_title = extract_newrelic_title(payload)
        severity = _extract_severity(payload)
        service = _extract_service(payload)
        alert_metadata = _build_alert_metadata(payload)

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                from utils.auth.stateless_auth import set_rls_context
                org_id = set_rls_context(cursor, conn, user_id, log_prefix="[NEWRELIC][WEBHOOK]")
                if not org_id:
                    return

                received_at = datetime.now(timezone.utc)

                issue_id_str = alert_metadata.get("issueId") or None
                priority_str = (payload.get("priority") or payload.get("severity") or "")[:20]
                state_str = (payload.get("state") or payload.get("currentState") or "")[:50]
                entity_names_str = ", ".join(
                    e.get("name", "") for e in payload.get("entitiesData", {}).get("entities", [])[:10]
                ) or None

                cursor.execute(
                    """
                    INSERT INTO newrelic_events
                    (user_id, org_id, issue_id, issue_title, priority, state, entity_names, payload, received_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (org_id, issue_id) WHERE issue_id IS NOT NULL
                    DO UPDATE
                    SET issue_title = EXCLUDED.issue_title,
                        priority = EXCLUDED.priority,
                        state = EXCLUDED.state,
                        payload = EXCLUDED.payload,
                        received_at = EXCLUDED.received_at
                    RETURNING id
                    """,
                    (
                        user_id,
                        org_id,
                        issue_id_str,
                        event_title,
                        priority_str,
                        state_str,
                        entity_names_str,
                        json.dumps(payload),
                        received_at,
                    ),
                )
                event_result = cursor.fetchone()
                event_id = event_result[0] if event_result else None
                conn.commit()

                if not event_id:
                    logger.error("[NEWRELIC][WEBHOOK] Failed to persist event for user %s", user_id)

                source_alert_id = event_id or f"nr-{int(received_at.timestamp())}"

                # --- Alert correlation ---

                try:
                    correlator = AlertCorrelator()
                    correlation_result = correlator.correlate(
                        cursor=cursor,
                        user_id=user_id,
                        source_type="newrelic",
                        source_alert_id=source_alert_id,
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
                            source_type="newrelic",
                            source_alert_id=source_alert_id,
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
                        "[NEWRELIC] Correlation check failed, proceeding with new incident: %s",
                        corr_exc,
                    )

                # --- Create new incident ---
                # New Relic exposes the issue's open time under several keys
                # depending on payload shape (workflow vs classic vs nested).
                alert_fired_at = extract_alert_fired_at(
                    payload,
                    [
                        "openedAt",
                        "issueAcceptedAt",
                        "createdAt",
                        "current_state.created_at",
                        "timestamp",
                    ],
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
                    RETURNING id
                    """,
                    (
                        user_id,
                        org_id,
                        "newrelic",
                        source_alert_id,
                        event_title,
                        service,
                        severity,
                        "investigating",
                        received_at,
                        json.dumps(alert_metadata),
                        alert_fired_at,
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
                            "newrelic",
                            source_alert_id,
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
                    logger.warning("[NEWRELIC] Failed to record primary alert: %s", e)

                if incident_id:
                    logger.info(
                        "[NEWRELIC][WEBHOOK] Created incident %s (alert=%s)",
                        incident_id, source_alert_id,
                    )

                    # Notify SSE connections
                    try:
                        from routes.incidents_sse import broadcast_incident_update_to_user_connections
                        broadcast_incident_update_to_user_connections(
                            user_id,
                            {"type": "incident_update", "incident_id": str(incident_id), "source": "newrelic"},
                        )
                    except Exception as e:
                        logger.warning("[NEWRELIC][WEBHOOK] Failed to notify SSE: %s", e)

                    # Trigger summary generation
                    from chat.background.summarization import generate_incident_summary
                    generate_incident_summary.delay(
                        incident_id=str(incident_id),
                        user_id=user_id,
                        source_type="newrelic",
                        alert_title=event_title or "New Relic Alert",
                        severity=severity,
                        service=service,
                        raw_payload=payload,
                        alert_metadata=alert_metadata,
                    )

                    # Trigger background RCA
                    try:
                        from chat.background.task import (
                            run_background_chat,
                            create_background_chat_session,
                            is_background_chat_allowed,
                        )

                        if not is_background_chat_allowed(user_id):
                            logger.info("[NEWRELIC][WEBHOOK] Skipping background RCA — rate limited for user %s", user_id)
                        else:
                            status_str = payload.get("state") or payload.get("currentState") or "unknown"
                            session_id = create_background_chat_session(
                                user_id=user_id,
                                title=f"RCA: {event_title}",
                                trigger_metadata={
                                    "source": "newrelic",
                                    "issueId": alert_metadata.get("issueId"),
                                    "status": status_str,
                                },
                                incident_id=str(incident_id),
                            )

                            rca_prompt, rail_text = build_newrelic_rca_prompt(payload, user_id=user_id)

                            task = run_background_chat.delay(
                                user_id=user_id,
                                session_id=session_id,
                                initial_message=rca_prompt,
                                trigger_metadata={
                                    "source": "newrelic",
                                    "issueId": alert_metadata.get("issueId"),
                                    "status": status_str,
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
                                "[NEWRELIC][WEBHOOK] Triggered background RCA for session %s (task_id=%s)",
                                session_id, task.id,
                            )
                    except Exception as e:
                        logger.error("[NEWRELIC][WEBHOOK] Failed to trigger RCA: %s", e)

    except Exception as exc:
        logger.exception("[NEWRELIC][WEBHOOK] Failed to process webhook payload")
        raise self.retry(exc=exc)
