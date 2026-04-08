"""Helper functions for Loki webhook alert processing.

Handles dual-format webhook payloads (Alertmanager v4 and Grafana Unified),
normalization, deduplication hashing, severity/service extraction, and
summary formatting.
"""

import hashlib
import logging
from datetime import datetime
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# Severity mapping from Prometheus/Loki convention to Aurora standard
_SEVERITY_MAP = {
    "critical": "critical",
    "warning": "high",
    "info": "low",
}


def detect_webhook_format(payload: Dict[str, Any]) -> str:
    """Detect whether a webhook payload is Alertmanager v4 or Grafana Unified.

    Args:
        payload: Raw webhook payload dict.

    Returns:
        "alertmanager_v4", "grafana_unified", or "unknown".
        Defaults to "alertmanager_v4" for unknown since both formats share
        the alerts[] array structure and Alertmanager is the more common
        path for Loki Ruler.
    """
    if payload.get("version") == "4":
        return "alertmanager_v4"
    if "orgId" in payload:
        return "grafana_unified"
    # Default to alertmanager_v4 -- both formats share alerts[] structure
    return "alertmanager_v4"


def extract_severity(alert: Dict[str, Any]) -> str:
    """Extract and normalize severity from an individual alert's labels.

    Args:
        alert: A single alert dict from the alerts[] array.

    Returns:
        Normalized severity string: "critical", "high", "low", raw value,
        or "unknown".
    """
    labels = alert.get("labels") or {}
    raw = labels.get("severity")
    if raw is None:
        return "unknown"
    return _SEVERITY_MAP.get(raw, raw)


def extract_service(alert: Dict[str, Any]) -> str:
    """Extract service identity from an individual alert's labels.

    Tries labels in order: service, job, namespace, instance.

    Args:
        alert: A single alert dict from the alerts[] array.

    Returns:
        Service name (truncated to 255 chars) or "unknown".
    """
    labels = alert.get("labels") or {}
    service = (
        labels.get("service")
        or labels.get("job")
        or labels.get("namespace")
        or labels.get("instance")
        or "unknown"
    )
    return str(service)[:255]


def normalize_loki_webhook(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Normalize a Loki webhook payload into a list of flat alert dicts.

    Handles both Alertmanager v4 and Grafana Unified formats, producing
    a common schema for each alert in the payload's alerts[] array.

    Args:
        payload: Raw webhook payload dict containing an alerts[] array.

    Returns:
        List of normalized alert dicts. Empty list if no alerts found.
    """
    fmt = detect_webhook_format(payload)
    alerts = payload.get("alerts", [])
    if not alerts:
        return []

    is_grafana = fmt == "grafana_unified"
    normalized = []

    for alert in alerts:
        labels = alert.get("labels") or {}
        annotations = alert.get("annotations") or {}

        entry = {
            "format": fmt,
            "alert_name": labels.get("alertname", "Unknown Alert"),
            "alert_state": alert.get("status", "unknown"),
            "severity": extract_severity(alert),
            "service": extract_service(alert),
            "labels": labels,
            "annotations": annotations,
            "starts_at": alert.get("startsAt"),
            "ends_at": alert.get("endsAt"),
            "generator_url": alert.get("generatorURL", ""),
            "fingerprint": alert.get("fingerprint", ""),
            "rule_group": payload.get("groupKey", "") if not is_grafana else "",
            "rule_name": labels.get("alertname", ""),
            "group_labels": payload.get("groupLabels", {}),
            "common_labels": payload.get("commonLabels", {}),
            "common_annotations": payload.get("commonAnnotations", {}),
            "external_url": payload.get("externalURL", ""),
            # Grafana-specific fields (None for alertmanager)
            "silence_url": alert.get("silenceURL") if is_grafana else None,
            "dashboard_url": alert.get("dashboardURL") if is_grafana else None,
            "panel_url": alert.get("panelURL") if is_grafana else None,
            "values": alert.get("values") if is_grafana else None,
        }
        normalized.append(entry)

    return normalized


def generate_alert_hash(
    user_id: str, normalized: Dict[str, Any], received_at: datetime
) -> str:
    """Generate a SHA-256 deduplication hash for a normalized alert.

    Uses fingerprint when available (preferred), otherwise falls back to
    alert_name + service combination.

    Args:
        user_id: The user receiving the alert.
        normalized: A single normalized alert dict from normalize_loki_webhook.
        received_at: Timestamp when the alert was received.

    Returns:
        First 64 hex characters of the SHA-256 hash.
    """
    fingerprint = normalized.get("fingerprint", "")
    alert_state = normalized.get("alert_state", "")

    if fingerprint:
        key_data = f"{user_id}:{fingerprint}:{alert_state}:{received_at.isoformat()}"
    else:
        alert_name = normalized.get("alert_name", "")
        service = normalized.get("service", "")
        key_data = f"{user_id}:{alert_name}:{service}:{alert_state}:{received_at.isoformat()}"

    return hashlib.sha256(key_data.encode()).hexdigest()[:64]


def format_alert_summary(normalized: Dict[str, Any]) -> str:
    """Format a one-line summary string for a normalized alert.

    Args:
        normalized: A single normalized alert dict from normalize_loki_webhook.

    Returns:
        Human-readable summary string.
    """
    alert_name = normalized.get("alert_name", "Unknown")
    alert_state = normalized.get("alert_state", "unknown")
    severity = normalized.get("severity", "unknown")
    service = normalized.get("service", "unknown")
    return f"{alert_name} [{alert_state}] severity={severity} service={service}"


def should_trigger_background_chat(user_id: str, payload: Dict[str, Any]) -> bool:
    """Determine if a background RCA chat should be triggered for this alert.

    Currently always returns True, matching the Netdata/Datadog pattern
    of triggering RCA for every incoming webhook.

    Args:
        user_id: The user ID receiving the alert.
        payload: The raw webhook payload.

    Returns:
        True (always trigger RCA).
    """
    return True
