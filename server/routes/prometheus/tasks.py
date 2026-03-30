"""Celery tasks for Prometheus Alertmanager webhook processing.

Processes Alertmanager webhook payloads and feeds them into Aurora's
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
    "error": "critical",
    "warning": "high",
    "info": "low",
    "none": "low",
}


def _extract_severity(labels: Dict[str, Any]) -> str:
    raw = str(labels.get("severity", "unknown")).lower()
    return _SEVERITY_MAP.get(raw, "medium")


def _extract_title(alert: Dict[str, Any]) -> str:
    annotations = alert.get("annotations", {})
    labels = alert.get("labels", {})
    return (
        annotations.get("summary")
        or annotations.get("description")
        or labels.get("alertname")
        or "Prometheus Alert"
    )


def _extract_service(labels: Dict[str, Any]) -> str:
    return str(
        labels.get("service")
        or labels.get("job")
        or labels.get("namespace")
        or labels.get("instance")
        or "unknown"
    )[:255]


def _build_alert_metadata(alert: Dict[str, Any], group_labels: Dict[str, Any]) -> Dict[str, Any]:
    meta: Dict[str, Any] = {}
    if labels := alert.get("labels"):
        meta["labels"] = labels
    if annotations := alert.get("annotations"):
        meta["annotations"] = annotations
    if group_labels:
        meta["groupLabels"] = group_labels
    if generator_url := alert.get("generatorURL"):
        meta["generatorURL"] = generator_url
    if fingerprint := alert.get("fingerprint"):
        meta["fingerprint"] = fingerprint
    return meta


def _should_trigger_rca(user_id: str) -> bool:
    return get_user_preference(user_id, "prometheus_rca_enabled", default=False)


def _build_rca_prompt(alert: Dict[str, Any], user_id: str | None = None) -> str:
    from chat.background.rca_prompt_builder import build_prometheus_rca_prompt
    return build_prometheus_rca_prompt(alert, user_id=user_id)


@celery_app.task(
    bind=True, max_retries=3, default_retry_delay=30,
    name="prometheus.process_webhook",
)
def process_prometheus_webhook(
    self,
    raw_payload: Dict[str, Any],
    metadata: Optional[Dict[str, Any]] = None,
    user_id: Optional[str] = None,
) -> None:
    """Process a Prometheus Alertmanager webhook payload."""
    received_at = datetime.now(timezone.utc)

    if not user_id:
        logger.warning("[PROMETHEUS] Received event with no user_id, skipping")
        return

    try:
        from utils.db.connection_pool import db_pool

        alerts = raw_payload.get("alerts", [])
        group_labels = raw_payload.get("groupLabels", {})

        if not alerts:
            logger.warning("[PROMETHEUS] Webhook payload has no alerts, skipping")
            return

        for alert in alerts:
            labels = alert.get("labels", {})
            alert_name = labels.get("alertname", "unknown")
            alert_status = alert.get("status", "firing")
            severity = _extract_severity(labels)
            title = _extract_title(alert)
            service = _extract_service(labels)
            fingerprint = alert.get("fingerprint")
            instance = labels.get("instance")
            alert_metadata = _build_alert_metadata(alert, group_labels)

            logger.info("[PROMETHEUS][ALERT][USER:%s] %s (%s)", user_id, title, alert_status)

            with db_pool.get_admin_connection() as conn, conn.cursor() as cursor:

                # 1. Store raw event
                cursor.execute(
                    """INSERT INTO prometheus_alerts
                       (user_id, alert_name, alert_status, alert_severity,
                        instance, group_name, fingerprint, labels, annotations,
                        generator_url, payload, received_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       RETURNING id""",
                    (
                        user_id, alert_name, alert_status,
                        labels.get("severity", "unknown"),
                        instance, group_labels.get("alertname"),
                        fingerprint, json.dumps(labels),
                        json.dumps(alert.get("annotations", {})),
                        alert.get("generatorURL"),
                        json.dumps(raw_payload), received_at,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    logger.error("[PROMETHEUS] INSERT returned no row for user %s", user_id)
                    continue
                alert_db_id = row[0]
                conn.commit()

                # Skip further processing for resolved alerts
                if alert_status == "resolved":
                    continue

                # 2. Run correlation
                try:
                    correlator = AlertCorrelator()
                    result = correlator.correlate(
                        cursor=cursor, user_id=user_id, source_type="prometheus",
                        source_alert_id=alert_db_id, alert_title=title,
                        alert_service=service, alert_severity=severity,
                        alert_metadata=alert_metadata,
                    )
                    if result.is_correlated:
                        handle_correlated_alert(
                            cursor=cursor, user_id=user_id, incident_id=result.incident_id,
                            source_type="prometheus", source_alert_id=alert_db_id,
                            alert_title=title, alert_service=service, alert_severity=severity,
                            correlation_result=result, alert_metadata=alert_metadata,
                            raw_payload=raw_payload,
                        )
                        conn.commit()
                        continue
                except Exception as corr_exc:
                    logger.warning("[PROMETHEUS] Correlation failed, proceeding: %s", corr_exc)

                # 3. Check if RCA is enabled
                if not _should_trigger_rca(user_id):
                    conn.commit()
                    logger.info("[PROMETHEUS] Stored for user %s (RCA disabled)", user_id)
                    continue

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
                    (user_id, "prometheus", alert_db_id, title, service,
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
                        (user_id, aurora_incident_id, "prometheus", alert_db_id, title,
                         service, severity, "primary", 1.0, json.dumps(alert_metadata)),
                    )
                    cursor.execute(
                        "UPDATE incidents SET affected_services = ARRAY[%s] WHERE id = %s",
                        (service, aurora_incident_id),
                    )
                conn.commit()

            if not aurora_incident_id:
                continue

            logger.info("[PROMETHEUS] Created incident %s for alert %s", aurora_incident_id, alert_db_id)

            # 5. Generate summary
            from chat.background.summarization import generate_incident_summary
            generate_incident_summary.delay(
                incident_id=str(aurora_incident_id), user_id=user_id, source_type="prometheus",
                alert_title=title, severity=severity, service=service,
                raw_payload=raw_payload, alert_metadata=alert_metadata,
            )

            # 6. Trigger RCA background chat
            try:
                from chat.background.task import (
                    run_background_chat, create_background_chat_session, is_background_chat_allowed,
                )
                if not is_background_chat_allowed(user_id):
                    logger.info("[PROMETHEUS] Skipping RCA - rate limited for user %s", user_id)
                    continue

                session_id = create_background_chat_session(
                    user_id=user_id,
                    title=f"RCA: {title}",
                    trigger_metadata={"source": "prometheus", "fingerprint": fingerprint},
                    incident_id=str(aurora_incident_id),
                )
                task = run_background_chat.delay(
                    user_id=user_id, session_id=session_id,
                    initial_message=_build_rca_prompt(alert, user_id=user_id),
                    trigger_metadata={"source": "prometheus", "fingerprint": fingerprint},
                    incident_id=str(aurora_incident_id),
                )
                with db_pool.get_admin_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE incidents SET rca_celery_task_id = %s WHERE id = %s",
                        (task.id, str(aurora_incident_id)),
                    )
                    conn.commit()
                logger.info("[PROMETHEUS] Triggered RCA for session %s (task=%s)", session_id, task.id)
            except Exception as chat_exc:
                logger.exception("[PROMETHEUS] Failed to trigger background chat: %s", chat_exc)

    except Exception as exc:
        logger.exception("[PROMETHEUS] Failed to process webhook for user %s", user_id)
        raise self.retry(exc=exc)
