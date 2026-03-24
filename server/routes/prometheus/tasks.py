"""Celery tasks for Prometheus/Alertmanager alert processing.

Processes Alertmanager alerts through Aurora's correlation pipeline,
incident creation, SSE broadcast, and RCA triggering.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from celery_config import celery_app
from services.correlation.alert_correlator import AlertCorrelator
from services.correlation import handle_correlated_alert
from utils.auth.stateless_auth import get_user_preference

logger = logging.getLogger(__name__)

_SEVERITY_MAP = {
    "critical": "critical",
    "warning": "high",
    "info": "low",
    "none": "medium",
}


def _extract_severity(alert: Dict[str, Any]) -> str:
    raw = str(alert.get("labels", {}).get("severity", "unknown")).lower()
    return _SEVERITY_MAP.get(raw, "medium")


def _extract_title(alert: Dict[str, Any]) -> str:
    annotations = alert.get("annotations", {})
    labels = alert.get("labels", {})
    return (
        annotations.get("summary")
        or annotations.get("description", "")[:200]
        or labels.get("alertname", "Prometheus Alert")
    )


def _extract_service(alert: Dict[str, Any]) -> str:
    labels = alert.get("labels", {})
    return str(
        labels.get("service")
        or labels.get("job")
        or labels.get("instance")
        or "unknown"
    )[:255]


def _build_alert_metadata(
    alert: Dict[str, Any],
    silences: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    meta: Dict[str, Any] = {
        "labels": alert.get("labels", {}),
        "annotations": alert.get("annotations", {}),
        "generatorURL": alert.get("generatorURL"),
    }
    if alert.get("startsAt"):
        meta["startsAt"] = alert["startsAt"]
    if alert.get("endsAt"):
        meta["endsAt"] = alert["endsAt"]
    if alert.get("fingerprint"):
        meta["fingerprint"] = alert["fingerprint"]
    if silences:
        matching = _find_matching_silences(alert, silences)
        if matching:
            meta["silences"] = matching
    return meta


def _find_matching_silences(
    alert: Dict[str, Any],
    silences: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Return silences whose matchers overlap with the alert's labels."""
    alert_labels = alert.get("labels", {})
    matching = []
    for silence in silences:
        if silence.get("status", {}).get("state") != "active":
            continue
        matchers = silence.get("matchers", [])
        if not matchers:
            continue
        all_match = True
        for m in matchers:
            name = m.get("name", "")
            value = m.get("value", "")
            is_regex = m.get("isRegex", False)
            is_equal = m.get("isEqual", True)
            label_val = alert_labels.get(name, "")
            if is_regex:
                import re
                try:
                    match = bool(re.fullmatch(value, label_val))
                except re.error:
                    match = False
            else:
                match = label_val == value
            if not is_equal:
                match = not match
            if not match:
                all_match = False
                break
        if all_match:
            matching.append({
                "id": silence.get("id"),
                "comment": silence.get("comment"),
                "createdBy": silence.get("createdBy"),
                "startsAt": silence.get("startsAt"),
                "endsAt": silence.get("endsAt"),
            })
    return matching


def _should_trigger_rca(user_id: str) -> bool:
    return get_user_preference(user_id, "prometheus_rca_enabled", default=False)


def _build_rca_prompt(alert: Dict[str, Any], user_id: str | None = None) -> str:
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    title = _extract_title(alert)
    severity = _extract_severity(alert)

    parts = [
        f"A Prometheus alert is firing: **{title}**",
        f"- Severity: {severity}",
        f"- Alert name: {labels.get('alertname', 'unknown')}",
    ]
    if labels.get("instance"):
        parts.append(f"- Instance: {labels['instance']}")
    if labels.get("job"):
        parts.append(f"- Job: {labels['job']}")
    if annotations.get("description"):
        parts.append(f"- Description: {annotations['description']}")
    if annotations.get("runbook_url"):
        parts.append(f"- Runbook: {annotations['runbook_url']}")

    parts.append("\nPlease investigate this alert and provide a root cause analysis.")
    parts.append("Use the connected Prometheus data source to query relevant metrics with PromQL.")
    return "\n".join(parts)


@celery_app.task(
    bind=True, max_retries=3, default_retry_delay=30,
    name="prometheus.process_alert",
)
def process_prometheus_alert(
    self,
    raw_alert: Dict[str, Any],
    silences: Optional[List[Dict[str, Any]]] = None,
    user_id: Optional[str] = None,
) -> None:
    """Process an Alertmanager alert through the full correlation pipeline."""
    received_at = datetime.now(timezone.utc)

    if not user_id:
        logger.warning("[PROMETHEUS] Received alert with no user_id, skipping")
        return

    try:
        from utils.db.connection_pool import db_pool

        alert = raw_alert
        fingerprint = alert.get("fingerprint", "")
        if not fingerprint:
            alertname = alert.get("labels", {}).get("alertname", "")
            if not alertname:
                logger.warning("[PROMETHEUS] Alert missing fingerprint and alertname, skipping")
                return
            fingerprint = alertname

        title = _extract_title(alert)
        severity = _extract_severity(alert)
        service = _extract_service(alert)
        alert_metadata = _build_alert_metadata(alert, silences=silences)

        logger.info("[PROMETHEUS][ALERT][USER:%s] %s", user_id, title)

        with db_pool.get_admin_connection() as conn, conn.cursor() as cursor:

            from utils.auth.stateless_auth import set_rls_context
            org_id = set_rls_context(cursor, conn, user_id, log_prefix="[PROMETHEUS]")
            if not org_id:
                return

            cursor.execute(
                """INSERT INTO prometheus_events
                   (user_id, fingerprint, alert_name, alert_status,
                    severity, instance, job, labels, annotations,
                    generator_url, payload, received_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                (
                    user_id,
                    fingerprint,
                    alert.get("labels", {}).get("alertname"),
                    alert.get("status", {}).get("state", "firing"),
                    alert.get("labels", {}).get("severity"),
                    alert.get("labels", {}).get("instance"),
                    alert.get("labels", {}).get("job"),
                    json.dumps(alert.get("labels", {})),
                    json.dumps(alert.get("annotations", {})),
                    alert.get("generatorURL"),
                    json.dumps(raw_alert),
                    received_at,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                logger.error("[PROMETHEUS] INSERT returned no row for user %s", user_id)
                return
            alert_db_id = row[0]
            conn.commit()

            # Run correlation
            try:
                correlator = AlertCorrelator()
                result = correlator.correlate(
                    cursor=cursor, user_id=user_id, source_type="prometheus",
                    source_alert_id=alert_db_id, alert_title=title,
                    alert_service=service, alert_severity=severity,
                    alert_metadata=alert_metadata, org_id=org_id,
                )
                if result.is_correlated:
                    handle_correlated_alert(
                        cursor=cursor, user_id=user_id, incident_id=result.incident_id,
                        source_type="prometheus", source_alert_id=alert_db_id,
                        alert_title=title, alert_service=service, alert_severity=severity,
                        correlation_result=result, alert_metadata=alert_metadata,
                        raw_payload=raw_alert, org_id=org_id,
                    )
                    conn.commit()
                    return
            except Exception as corr_exc:
                logger.warning("[PROMETHEUS] Correlation failed, proceeding: %s", corr_exc)

            if not _should_trigger_rca(user_id):
                conn.commit()
                logger.info("[PROMETHEUS] Stored for user %s (RCA disabled)", user_id)
                return

            # Create new incident
            cursor.execute(
                """INSERT INTO incidents
                   (user_id, org_id, source_type, source_alert_id, alert_title, alert_service,
                    severity, status, started_at, alert_metadata)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (org_id, source_type, source_alert_id, user_id) DO UPDATE
                   SET updated_at = CURRENT_TIMESTAMP,
                       started_at = CASE WHEN incidents.status != 'analyzed'
                                    THEN EXCLUDED.started_at ELSE incidents.started_at END,
                       alert_metadata = EXCLUDED.alert_metadata
                   RETURNING id""",
                (user_id, org_id, "prometheus", alert_db_id, title, service,
                 severity, "investigating", received_at, json.dumps(alert_metadata)),
            )
            incident_row = cursor.fetchone()
            aurora_incident_id = incident_row[0] if incident_row else None

            if aurora_incident_id:
                cursor.execute(
                    """INSERT INTO incident_alerts
                       (user_id, org_id, incident_id, source_type, source_alert_id, alert_title,
                        alert_service, alert_severity, correlation_strategy, correlation_score, alert_metadata)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (user_id, org_id, aurora_incident_id, "prometheus", alert_db_id, title,
                     service, severity, "primary", 1.0, json.dumps(alert_metadata)),
                )
                cursor.execute(
                    "UPDATE incidents SET affected_services = ARRAY[%s] WHERE id = %s",
                    (service, aurora_incident_id),
                )
            conn.commit()

        if not aurora_incident_id:
            return

        logger.info("[PROMETHEUS] Created incident %s for alert %s", aurora_incident_id, alert_db_id)

        from chat.background.summarization import generate_incident_summary
        generate_incident_summary.delay(
            incident_id=str(aurora_incident_id), user_id=user_id, source_type="prometheus",
            alert_title=title, severity=severity, service=service,
            raw_payload=raw_alert, alert_metadata=alert_metadata,
        )

        try:
            from chat.background.task import (
                run_background_chat, create_background_chat_session, is_background_chat_allowed,
            )
            if not is_background_chat_allowed(user_id):
                logger.info("[PROMETHEUS] Skipping RCA - rate limited for user %s", user_id)
                return

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
        logger.exception("[PROMETHEUS] Failed to process alert for user %s", user_id)
        raise self.retry(exc=exc)
