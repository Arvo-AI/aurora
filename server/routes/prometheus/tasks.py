"""Celery tasks for Prometheus/Alertmanager webhook processing."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from celery_config import celery_app
from services.general_alert_task import AlertPipelineInput, process_alert_pipeline
from utils.payload_timestamp import extract_alert_fired_at

logger = logging.getLogger(__name__)


def extract_prometheus_title(alert: Dict[str, Any], default: str = "Prometheus Alert") -> str:
    """Extract alert title from a single Alertmanager alert entry.

    Alertmanager payloads nest individual alerts under alerts[].
    Each alert has labels (alertname, instance, job, etc.) and annotations (summary, description).
    """
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})

    # annotations.summary is the human-readable title
    if annotations.get("summary"):
        return str(annotations["summary"])

    # alertname label is always present
    alertname = labels.get("alertname")
    if alertname:
        instance = labels.get("instance", "")
        if instance:
            return f"{alertname} ({instance})"
        return str(alertname)

    # Fallback to annotation description
    if annotations.get("description"):
        desc = str(annotations["description"])
        return desc[:200] if len(desc) > 200 else desc

    return default


_SEVERITY_MAP = {
    "critical": "critical",
    "high": "high",
    "warning": "high",
    "medium": "medium",
    "info": "medium",
    "informational": "medium",
    "low": "low",
    "none": "low",
}


def _extract_service(alert: Dict[str, Any]) -> str:
    """Extract affected service/entity name from Alertmanager alert labels."""
    labels = alert.get("labels", {})

    for key in ("service", "job", "app", "application", "namespace", "container", "instance"):
        val = labels.get(key)
        if val:
            return str(val)[:255]

    return "unknown"


def _build_alert_metadata(alert: Dict[str, Any], group_labels: Dict[str, Any]) -> Dict[str, Any]:
    """Build metadata dict from raw alert for the correlator."""
    meta = dict(alert)
    if group_labels:
        meta["groupLabels"] = group_labels
    if "annotations" in meta and meta["annotations"].get("description"):
        meta["annotations"] = {**meta["annotations"], "description": str(meta["annotations"]["description"])[:1000]}
    return meta


def _make_persist_event(
    user_id: str,
    alert: Dict[str, Any],
    severity: str,
    service: str,
):
    """Create a persist_event callback for the shared pipeline.

    Returns a closure that inserts into prometheus_events and returns the event_id.
    """
    labels = alert.get("labels", {})
    alert_name = labels.get("alertname", "unknown")
    alert_status = alert.get("status", "unknown")

    def persist_event(cursor, org_id: str, received_at: datetime) -> Optional[int]:
        cursor.execute(
            """
            INSERT INTO prometheus_events
            (user_id, org_id, alert_name, alert_status, severity, service, labels, payload, received_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                user_id,
                org_id,
                alert_name,
                alert_status,
                severity,
                service,
                json.dumps(labels),
                json.dumps(alert),
                received_at,
            ),
        )
        row = cursor.fetchone()
        return row[0] if row else None

    return persist_event


@celery_app.task(
    bind=True, max_retries=3, default_retry_delay=30, name="prometheus.process_alert"
)
def process_prometheus_alert(
    self,
    payload: Dict[str, Any],
    metadata: Optional[Dict[str, Any]] = None,
    user_id: str = "",
) -> None:
    """Background processor for Alertmanager webhook payloads.

    Alertmanager sends batches of alerts grouped by labels.
    Each alert in the batch is processed independently for correlation/incident creation.
    """
    alerts = payload.get("alerts", [])
    group_labels = payload.get("groupLabels", {})

    logger.info(
        "[PROMETHEUS][WEBHOOK][USER:%s] Processing %d alert(s), group=%s",
        user_id or "unknown", len(alerts), group_labels,
    )

    try:
        if not alerts:
            logger.info("[PROMETHEUS][WEBHOOK] No alerts in payload; skipping")
            return

        # Process only firing alerts for incidents (resolved still get persisted)
        firing_alerts = [a for a in alerts if a.get("status") == "firing"]
        if not firing_alerts:
            logger.info("[PROMETHEUS][WEBHOOK] All alerts resolved; skipping RCA")
            firing_alerts = alerts

        for alert in firing_alerts:
            _process_single_alert(alert, group_labels, payload, user_id)

    except Exception as exc:
        logger.exception("[PROMETHEUS][WEBHOOK] Failed to process webhook payload")
        raise self.retry(exc=exc)


def _process_single_alert(
    alert: Dict[str, Any],
    group_labels: Dict[str, Any],
    full_payload: Dict[str, Any],
    user_id: str,
) -> None:
    """Process a single alert from the Alertmanager batch via the shared pipeline."""
    event_title = extract_prometheus_title(alert)
    severity = _SEVERITY_MAP.get(alert.get("labels", {}).get("severity", "").lower(), "high")
    service = _extract_service(alert)
    alert_metadata = _build_alert_metadata(alert, group_labels)
    is_firing = alert.get("status") == "firing"

    alert_fired_at = extract_alert_fired_at(alert, ["startsAt", "activeAt"])

    pipeline_input = AlertPipelineInput(
        source_type="prometheus",
        user_id=user_id,
        event_title=event_title,
        severity=severity,
        service=service,
        alert_metadata=alert_metadata,
        raw_payload=alert,
        persist_event=_make_persist_event(user_id, alert, severity, service),
        alert_fired_at=alert_fired_at,
        trigger_metadata={
            "source": "prometheus",
            "alertname": alert_metadata.get("alertname"),
            "status": alert.get("status"),
        },
        # Resolved alerts are persisted but don't create incidents
        skip_incident_creation=not is_firing,
    )

    process_alert_pipeline(pipeline_input)
