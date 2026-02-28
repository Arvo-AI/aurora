"""Celery tasks for BigPanda webhook processing.

Processes BigPanda incident webhooks and feeds them into Aurora's
correlation pipeline. Full implementation in a follow-up PR.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from celery_config import celery_app

logger = logging.getLogger(__name__)


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
    """Process a BigPanda webhook payload.

    Stores the raw event in bigpanda_events. Correlation, incident creation,
    SSE broadcast, and RCA triggering will be added in a follow-up PR.
    """
    received_at = datetime.now(timezone.utc)

    if not user_id:
        logger.warning("[BIGPANDA] Received event with no user_id, skipping")
        return

    try:
        from utils.db.connection_pool import db_pool

        # Extract incident from payload (handle both direct and nested shapes)
        incident = raw_payload
        if "incident" in raw_payload:
            incident = raw_payload["incident"]

        incident_id = incident.get("id")
        if not incident_id:
            logger.warning("[BIGPANDA] Payload missing incident ID, skipping")
            return

        bp_status = incident.get("status", "active")
        event_type = f"incident.{bp_status}"
        alerts_raw = incident.get("alerts") or []
        if isinstance(alerts_raw, list):
            alerts = alerts_raw
        elif isinstance(alerts_raw, dict):
            alerts = [alerts_raw]
        else:
            alerts = []
        first_alert = alerts[0] if alerts and isinstance(alerts[0], dict) else {}

        incident_title = (
            first_alert.get("description")
            or first_alert.get("condition_name")
            or f"BigPanda Incident {incident_id}"
        )

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO bigpanda_events
                    (user_id, event_type, incident_id, incident_title, incident_status,
                     incident_severity, primary_property, secondary_property,
                     source_system, child_alert_count, payload, received_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        user_id,
                        event_type,
                        incident_id,
                        incident_title[:500] if incident_title else None,
                        bp_status,
                        incident.get("severity"),
                        first_alert.get("primary_property"),
                        first_alert.get("secondary_property"),
                        first_alert.get("source_system"),
                        len(alerts),
                        json.dumps(raw_payload),
                        received_at,
                    ),
                )
                event_row = cursor.fetchone()
            conn.commit()

        event_db_id = event_row[0] if event_row else None
        logger.info(
            "[BIGPANDA] Stored event %s for user %s (incident=%s, status=%s, alerts=%d)",
            event_db_id, user_id, incident_id, bp_status, len(alerts),
        )

    except Exception as exc:
        logger.exception("[BIGPANDA] Failed to process webhook payload")
        raise self.retry(exc=exc)
