"""Shared alert-to-incident processing pipeline.

Encapsulates the common flow executed by every connector webhook task:
  1. Get DB connection, set RLS context
  2. Persist the connector-specific event via a caller-supplied callback
  3. Correlate against open incidents (AlertCorrelator)
  4. If correlated → delegate to handle_correlated_alert()
  5. If not correlated → create new incident, link via incident_alerts
  6. Trigger background RCA (build_rca_prompt + run_background_chat)

Each connector only provides extraction logic and a `persist_event` callback;
the pipeline owns everything else.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

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
from utils.auth.stateless_auth import set_rls_context
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)


@dataclass
class AlertPipelineInput:
    """All connector-agnostic parameters for the shared pipeline.

    Attributes:
        source_type: Connector identifier (e.g. "prometheus", "datadog").
        user_id: Owning user (tenant).
        event_title: Human-readable alert title.
        severity: Normalized severity string.
        service: Affected service or entity name.
        alert_metadata: Structured metadata for the correlator.
        raw_payload: Full webhook payload (for RCA context).
        persist_event: Callback ``(cursor, org_id, received_at) -> event_id``
            that inserts into the connector's event table.
        alert_fired_at: Optional timestamp when the alert originally fired
            (used for MTTD calculation on incidents).
        trigger_metadata: Optional dict passed to RCA chat session creation
            for traceability (e.g. monitor_id, alertname).
        skip_incident_creation: If True, persist the event but skip
            correlation/incident/RCA (e.g. for resolved alerts).
    """

    source_type: str
    user_id: str
    event_title: str
    severity: str
    service: str
    alert_metadata: Dict[str, Any]
    raw_payload: Dict[str, Any]
    persist_event: Callable
    alert_fired_at: Optional[datetime] = None
    trigger_metadata: Optional[Dict[str, Any]] = None
    skip_incident_creation: bool = False


def process_alert_pipeline(input: AlertPipelineInput) -> Optional[str]:
    """Execute the full alert→incident pipeline.

    Must be called from a Celery task context (no Flask request context).
    Returns the incident_id if a new incident was created, None otherwise.
    """
    log_prefix = f"[{input.source_type.upper()}][PIPELINE]"

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            org_id = set_rls_context(
                cursor, conn, input.user_id, log_prefix=log_prefix
            )
            if not org_id:
                return None

            received_at = datetime.now(timezone.utc)

            # Step 1: Persist connector-specific event
            event_id = input.persist_event(cursor, org_id, received_at)
            conn.commit()

            if not event_id:
                logger.error(
                    "%s Failed to persist event for user %s",
                    log_prefix, input.user_id,
                )
                return None

            # Early exit — event stored but no incident processing needed
            if input.skip_incident_creation:
                return None

            # Step 2: Alert correlation
            source_alert_id = event_id
            try:
                correlator = AlertCorrelator()
                correlation_result = correlator.correlate(
                    cursor=cursor,
                    user_id=input.user_id,
                    source_type=input.source_type,
                    source_alert_id=source_alert_id,
                    alert_title=input.event_title,
                    alert_service=input.service,
                    alert_severity=input.severity,
                    alert_metadata=input.alert_metadata,
                    org_id=org_id,
                )

                # Correlated → attach to existing incident and return
                if correlation_result.is_correlated:
                    handle_correlated_alert(
                        cursor=cursor,
                        user_id=input.user_id,
                        incident_id=correlation_result.incident_id,
                        source_type=input.source_type,
                        source_alert_id=source_alert_id,
                        alert_title=input.event_title,
                        alert_service=input.service,
                        alert_severity=input.severity,
                        correlation_result=correlation_result,
                        alert_metadata=input.alert_metadata,
                        raw_payload=input.raw_payload,
                        org_id=org_id,
                    )
                    conn.commit()
                    return None
            except Exception as corr_exc:
                logger.warning(
                    "%s Correlation check failed, proceeding with new incident: %s",
                    log_prefix, corr_exc,
                )

            # Step 3: Create new incident
            incident_id = _create_incident(
                cursor=cursor,
                conn=conn,
                input=input,
                org_id=org_id,
                source_alert_id=source_alert_id,
                received_at=received_at,
                log_prefix=log_prefix,
            )

            if not incident_id:
                return None

            # Step 4: Link primary alert via incident_alerts
            _link_primary_alert(
                cursor=cursor,
                conn=conn,
                input=input,
                org_id=org_id,
                incident_id=incident_id,
                source_alert_id=source_alert_id,
                log_prefix=log_prefix,
            )

            # Step 5: Notify SSE
            try:
                broadcast_incident_update_to_user_connections(
                    input.user_id,
                    {
                        "type": "incident_update",
                        "incident_id": str(incident_id),
                        "source": input.source_type,
                    },
                    org_id=org_id,
                )
            except Exception as e:
                logger.warning("%s Failed to notify SSE: %s", log_prefix, e)

            # Step 6: Trigger summary generation
            generate_incident_summary.delay(
                incident_id=str(incident_id),
                user_id=input.user_id,
                source_type=input.source_type,
                alert_title=input.event_title or f"{input.source_type.capitalize()} Alert",
                severity=input.severity,
                service=input.service,
                raw_payload=input.raw_payload,
                alert_metadata=input.alert_metadata,
            )

            # Step 7: Trigger background RCA
            _trigger_background_rca(
                cursor=cursor,
                conn=conn,
                input=input,
                incident_id=incident_id,
                log_prefix=log_prefix,
            )

            return incident_id


def _create_incident(
    cursor,
    conn,
    input: AlertPipelineInput,
    org_id: str,
    source_alert_id: int,
    received_at: datetime,
    log_prefix: str,
) -> Optional[str]:
    """Insert or upsert a new incident record. Returns the incident ID."""
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
            input.user_id,
            org_id,
            input.source_type,
            source_alert_id,
            input.event_title,
            input.service,
            input.severity,
            "investigating",
            received_at,
            json.dumps(input.alert_metadata),
            input.alert_fired_at,
        ),
    )
    row = cursor.fetchone()
    incident_id = str(row[0]) if row else None
    conn.commit()

    if incident_id:
        logger.info(
            "%s Created incident %s (alert=%s)",
            log_prefix, incident_id, source_alert_id,
        )

    return incident_id


def _link_primary_alert(
    cursor,
    conn,
    input: AlertPipelineInput,
    org_id: str,
    incident_id: str,
    source_alert_id: int,
    log_prefix: str,
) -> None:
    """Record the primary alert in incident_alerts and set affected_services."""
    try:
        cursor.execute(
            """INSERT INTO incident_alerts
               (user_id, org_id, incident_id, source_type, source_alert_id, alert_title, alert_service,
                alert_severity, correlation_strategy, correlation_score, alert_metadata)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                input.user_id,
                org_id,
                incident_id,
                input.source_type,
                source_alert_id,
                input.event_title,
                input.service,
                input.severity,
                "primary",
                1.0,
                json.dumps(input.alert_metadata),
            ),
        )
        cursor.execute(
            "UPDATE incidents SET affected_services = ARRAY[%s] WHERE id = %s",
            (input.service, incident_id),
        )
        conn.commit()
    except Exception as e:
        logger.warning("%s Failed to record primary alert: %s", log_prefix, e)


def _trigger_background_rca(
    cursor,
    conn,
    input: AlertPipelineInput,
    incident_id: str,
    log_prefix: str,
) -> None:
    """Build RCA prompt and enqueue the background chat task."""
    try:
        if not is_background_chat_allowed(input.user_id):
            logger.info(
                "%s Skipping background RCA — rate limited for user %s",
                log_prefix, input.user_id,
            )
            return

        session_id = create_background_chat_session(
            user_id=input.user_id,
            title=f"RCA: {input.event_title}",
            trigger_metadata=input.trigger_metadata or {"source": input.source_type},
            incident_id=str(incident_id),
        )

        rca_prompt, rail_text = build_rca_prompt(
            input.source_type,
            input.event_title,
            input.raw_payload,
            user_id=input.user_id,
        )

        task = run_background_chat.delay(
            user_id=input.user_id,
            session_id=session_id,
            initial_message=rca_prompt,
            trigger_metadata=input.trigger_metadata or {"source": input.source_type},
            incident_id=str(incident_id),
            rail_text=rail_text,
        )

        cursor.execute(
            "UPDATE incidents SET rca_celery_task_id = %s WHERE id = %s",
            (task.id, str(incident_id)),
        )
        conn.commit()

        logger.info(
            "%s Triggered background RCA for session %s (task_id=%s)",
            log_prefix, session_id, task.id,
        )
    except Exception as e:
        logger.error("%s Failed to trigger RCA: %s", log_prefix, e)
