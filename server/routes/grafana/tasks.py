"""Celery tasks for Grafana alert webhook processing.

Each webhook contains an ``alerts[]`` array of individual alert instances. We process
each alert separately by fingerprint (hash of rule + labels):
- Firing alerts: create an incident and trigger RCA.
- Resolved alerts: match to the original incident by fingerprint, skip RCA.

See docs/oss/GRAFANA_ALERTS.md for edge cases and matching details.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from celery_config import celery_app
from services.correlation.alert_correlator import AlertCorrelator
from services.correlation import handle_correlated_alert

logger = logging.getLogger(__name__)


def _is_resolved_alert(payload: Dict[str, Any]) -> bool:
    """If state is ok, all individual alerts in the webhook are resolved."""
    state = (payload.get("state") or "").lower()
    status = (payload.get("status") or "").lower()
    return state == "ok" or status == "resolved"


def _should_trigger_background_chat(user_id: str, payload: Dict[str, Any]) -> bool:
    """Determine if a background chat should be triggered for this alert.

    Args:
        user_id: The user ID receiving the alert
        payload: The Grafana alert payload

    Returns:
        True if a background chat should be triggered
    """
    return not _is_resolved_alert(payload)


def _build_rca_prompt_from_alert(
    payload: Dict[str, Any], user_id: Optional[str] = None
) -> str:
    """Build an RCA analysis prompt from a Grafana alert payload.

    Args:
        payload: The Grafana alert payload
        user_id: Optional user ID for Aurora Learn context injection

    Returns:
        A prompt string for the background chat agent
    """
    title = payload.get("title") or payload.get("ruleName") or "Unknown Alert"
    state = payload.get("state") or payload.get("status") or "unknown"
    message = (
        payload.get("message")
        or payload.get("annotations", {}).get("description")
        or ""
    )

    # Extract labels for context
    labels = payload.get("commonLabels", {}) or payload.get("labels", {})
    labels_str = ", ".join(f"{k}={v}" for k, v in labels.items()) if labels else "none"

    # Extract any values/metrics
    values = payload.get("values") or payload.get("evalMatches", [])
    values_str = ""
    if values:
        if isinstance(values, list):
            values_str = "\n".join(f"  - {v}" for v in values[:5])  # Limit to 5
        elif isinstance(values, dict):
            values_str = "\n".join(f"  - {k}: {v}" for k, v in list(values.items())[:5])

    # Build the prompt parts separately to avoid f-string backslash issues
    prompt_parts = [
        "A Grafana alert has been triggered and requires Root Cause Analysis.",
        "",
        "ALERT DETAILS:",
        f"- Title: {title}",
        f"- State: {state}",
        f"- Labels: {labels_str}",
    ]

    if message:
        prompt_parts.append(f"- Message: {message}")

    if values_str:
        prompt_parts.append("- Values/Metrics:")
        prompt_parts.append(values_str)

    # Add Aurora Learn context if available
    try:
        from chat.background.rca_prompt_builder import inject_aurora_learn_context

        service = labels.get("service") or labels.get("job") or ""
        inject_aurora_learn_context(prompt_parts, user_id, title, service, "grafana")
    except Exception as e:
        logger.warning(f"[AURORA LEARN] Failed to get context: {e}")

    return "\n".join(prompt_parts)


def _extract_severity(payload: Dict[str, Any]) -> str:
    """Extract severity from Grafana alert payload.

    Grafana states are: alerting, ok, pending, no_data, paused
    Severity should come from labels (e.g., severity: critical).

    If no severity label exists, map state to severity:
    - alerting: critical (active alert)
    - ok: low (resolved)
    - pending: high (about to fire)
    - no_data/paused: unknown
    """
    # Check for severity in labels first
    labels = payload.get("commonLabels", {}) or payload.get("labels", {})
    if "severity" in labels:
        severity = str(labels["severity"]).lower()
        if severity in ("critical", "high", "medium", "low"):
            return severity

    # Map state to severity as fallback
    state = (payload.get("state") or payload.get("status") or "").lower()
    if state == "alerting":
        return "critical"
    elif state == "pending":
        return "high"
    elif state == "ok":
        return "low"

    return "unknown"


def _extract_service(payload: Dict[str, Any]) -> str:
    """Extract service name from Grafana payload."""
    # Try to get from labels
    labels = payload.get("commonLabels", {}) or payload.get("labels", {})
    service = (
        labels.get("service") or labels.get("job") or labels.get("alertname", "unknown")
    )
    return str(service)[:255]  # Truncate to fit DB column


def _merge_alert_into_payload(payload: Dict[str, Any], alert: Dict[str, Any]) -> Dict[str, Any]:
    """Overlay a single alert's fields onto the webhook envelope so existing helpers
    (which expect a webhook-shaped dict) can process each alert individually."""
    merged = dict(payload)
    merged["fingerprint"] = alert.get("fingerprint")
    merged["ruleUID"] = alert.get("ruleUID") or alert.get("ruleUid")
    if alert.get("labels"):
        merged["commonLabels"] = {**merged.get("commonLabels", {}), **alert["labels"]}
    if alert.get("annotations"):
        merged["commonAnnotations"] = {**merged.get("commonAnnotations", {}), **alert["annotations"]}
    if alert.get("values"):
        merged["values"] = alert["values"]
    for key in ("dashboardURL", "panelURL", "silenceURL", "imageURL", "generatorURL"):
        if alert.get(key):
            merged[key] = alert[key]
    merged["status"] = alert.get("status") or merged.get("status")
    merged["state"] = alert.get("state") or merged.get("state")
    return merged


def _format_alert_summary(payload: Dict[str, Any]) -> str:
    title = payload.get("title") or payload.get("ruleName") or "Unnamed Alert"
    state = payload.get("state") or payload.get("status") or "unknown"
    rule_uid = payload.get("ruleUid") or payload.get("ruleId")
    return f"{title} [{state}]" + (f" (rule={rule_uid})" if rule_uid else "")


def _safe_json_dump(data: Dict[str, Any]) -> str:
    try:
        return json.dumps(data, ensure_ascii=False)
    except Exception:  # pragma: no cover - defensive
        return str(data)


@celery_app.task(
    bind=True, max_retries=3, default_retry_delay=30, name="grafana.process_alert"
)
def process_grafana_alert(
    self,
    payload: Dict[str, Any],
    metadata: Optional[Dict[str, Any]] = None,
    user_id: Optional[str] = None,
) -> None:
    """Background processor for Grafana alert webhooks.

    Args:
        payload: Raw webhook JSON payload received from Grafana.
        metadata: Auxiliary information captured at the HTTP layer (headers, user context, etc.).
        user_id: Aurora user ID this alert belongs to.
    """
    try:
        summary = _format_alert_summary(payload)
        logger.info("[GRAFANA][ALERT][USER:%s] %s", user_id or "unknown", summary)

        logger.debug("[GRAFANA][ALERT] full payload=%s", _safe_json_dump(payload))

        # Persist alert to database if user_id is provided
        if user_id:
            from utils.db.connection_pool import db_pool

            try:
                with db_pool.get_admin_connection() as conn:
                    with conn.cursor() as cursor:
                        received_at = datetime.now(timezone.utc)
                        # Extract relevant fields from Grafana payload
                        alert_uid = payload.get("ruleUID") or payload.get("ruleUid")
                        if not alert_uid and payload.get("alerts"):
                            first_alert = payload["alerts"][0] #Alert uid is the same for all alerts in the webhook
                            alert_uid = first_alert.get("ruleUID") or first_alert.get("ruleUid")
                        alert_title = payload.get("title") or payload.get(
                            "commonLabels", {}
                        ).get("alertname")
                        alert_state = payload.get("state") or payload.get("status")
                        rule_name = payload.get("ruleName") or payload.get(
                            "commonLabels", {}
                        ).get("rulename")
                        rule_url = payload.get("ruleUrl") or payload.get("ruleURL")
                        dashboard_url = payload.get("dashboardURL") or payload.get(
                            "dashboardUrl"
                        )
                        panel_url = payload.get("panelURL") or payload.get("panelUrl")

                        cursor.execute(
                            """
                            INSERT INTO grafana_alerts 
                            (user_id, alert_uid, alert_title, alert_state, rule_name, rule_url, dashboard_url, panel_url, payload, received_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            RETURNING id
                            """,
                            (
                                user_id,
                                alert_uid,
                                alert_title,
                                alert_state,
                                rule_name,
                                rule_url,
                                dashboard_url,
                                panel_url,
                                json.dumps(payload),
                                received_at,
                            ),
                        )
                        alert_result = cursor.fetchone()
                        alert_id = alert_result[0] if alert_result else None
                        conn.commit()

                        if not alert_id:
                            logger.error(
                                "[GRAFANA][ALERT] Failed to get alert_id for user %s",
                                user_id,
                            )
                            return

                        logger.info(
                            "[GRAFANA][ALERT] Stored alert in database for user %s",
                            user_id,
                        )

                        # A single Grafana webhook can contain multiple alerts (different fingerprints).
                        # Each gets its own incident/resolution handling.
                        individual_alerts = payload.get("alerts") or [{}]
                        for single_alert in individual_alerts:
                            alert_payload = _merge_alert_into_payload(payload, single_alert) if single_alert else payload
                            fingerprint = single_alert.get("fingerprint")

                            # If resolved webhook, find the original incident by fingerprint and attach this alert to it as a correlated event and skip RCA.
                            if _is_resolved_alert(alert_payload):
                                original_incident_id = None

                                if fingerprint:
                                    # Check if fingerprint matches an incident directly (ordered by most recent if we need to reinvestigate same alert as we did before)
                                    cursor.execute(
                                        """SELECT id FROM incidents
                                           WHERE user_id = %s AND source_type = 'grafana'
                                             AND alert_metadata::jsonb ->> 'fingerprint' = %s
                                           ORDER BY started_at DESC LIMIT 1""",
                                        (user_id, fingerprint),
                                    )
                                    row = cursor.fetchone()
                                    if row:
                                        original_incident_id = row[0]

                                    # Fallback: if the firing alert was correlated to an existing
                                    # incident (via AlertCorrelator), its fingerprint lives in
                                    # incident_alerts.alert_metadata, not in incidents.alert_metadata.
                                    # Ex: Alert B is corrlated to alert A. Alert B's resolve won't find 
                                    # alert B's fingerprint in incidents.alert_metadata, but in incident_alerts.alert_metadata.
                                    if not original_incident_id:
                                        cursor.execute(
                                            """SELECT ia.incident_id FROM incident_alerts ia
                                               JOIN incidents i ON i.id = ia.incident_id
                                               WHERE ia.user_id = %s AND ia.source_type = 'grafana'
                                                 AND ia.alert_metadata::jsonb ->> 'fingerprint' = %s
                                               ORDER BY ia.received_at DESC LIMIT 1""",
                                            (user_id, fingerprint),
                                        )
                                        row = cursor.fetchone()
                                        if row:
                                            original_incident_id = row[0]

                                # Attach resolved alert to the original incident
                                if original_incident_id:
                                    cursor.execute(
                                        """INSERT INTO incident_alerts
                                           (user_id, incident_id, source_type, source_alert_id, alert_title, alert_service,
                                            alert_severity, correlation_strategy, correlation_score, alert_metadata)
                                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                                        (
                                            user_id, original_incident_id, "grafana", alert_id,
                                            alert_title, _extract_service(alert_payload),
                                            _extract_severity(alert_payload), "resolved_webhook", 1.0,
                                            json.dumps({"resolved_webhook": True, "fingerprint": fingerprint}),
                                        ),
                                    )
                                    conn.commit()
                                    logger.info(
                                        "[GRAFANA][ALERT] Correlated resolved alert (fp=%s) to incident %s",
                                        fingerprint, original_incident_id,
                                    )
                                else:
                                    logger.info(
                                        "[GRAFANA][ALERT] Resolved alert for user %s, no matching incident (fp=%s). Skipping.",
                                        user_id, fingerprint,
                                    )
                                continue

                            # -- Firing: create incident and trigger RCA --
                            severity = _extract_severity(alert_payload)
                            service = _extract_service(alert_payload)

                            # Build alert metadata from the per-alert payload
                            alert_metadata = {}
                            if dashboard_url:
                                alert_metadata["dashboardUrl"] = dashboard_url
                            if panel_url:
                                alert_metadata["panelUrl"] = panel_url
                            if rule_url:
                                alert_metadata["alertUrl"] = rule_url

                            a_labels = alert_payload.get("commonLabels") or alert_payload.get("labels") or {}
                            if a_labels:
                                alert_metadata["labels"] = a_labels

                            a_annotations = alert_payload.get("commonAnnotations") or alert_payload.get("annotations") or {}
                            if a_annotations.get("summary"):
                                alert_metadata["summary"] = a_annotations["summary"]
                            if a_annotations.get("description"):
                                alert_metadata["description"] = a_annotations["description"]
                            if a_annotations.get("runbook_url"):
                                alert_metadata["runbookUrl"] = a_annotations["runbook_url"]

                            if alert_payload.get("values"):
                                alert_metadata["values"] = alert_payload["values"]
                            if alert_payload.get("imageURL"):
                                alert_metadata["imageUrl"] = alert_payload["imageURL"]
                            if alert_payload.get("silenceURL"):
                                alert_metadata["silenceUrl"] = alert_payload["silenceURL"]
                            if fingerprint:
                                alert_metadata["fingerprint"] = fingerprint
                            per_alert_rule_uid = single_alert.get("ruleUID") or single_alert.get("ruleUid")
                            if per_alert_rule_uid:
                                alert_metadata["ruleUID"] = per_alert_rule_uid

                            # Try to correlate with an existing open incident (time/similarity/topology based)
                            try:
                                correlator = AlertCorrelator()
                                correlation_result = correlator.correlate(
                                    cursor=cursor, user_id=user_id, source_type="grafana",
                                    source_alert_id=alert_id, alert_title=alert_title,
                                    alert_service=service, alert_severity=severity,
                                    alert_metadata=alert_metadata,
                                )
                                if correlation_result.is_correlated:
                                    handle_correlated_alert(
                                        cursor=cursor, user_id=user_id,
                                        incident_id=correlation_result.incident_id,
                                        source_type="grafana", source_alert_id=alert_id,
                                        alert_title=alert_title, alert_service=service,
                                        alert_severity=severity,
                                        correlation_result=correlation_result,
                                        alert_metadata=alert_metadata, raw_payload=alert_payload,
                                    )
                                    conn.commit()
                                    continue
                            except Exception as corr_exc:
                                logger.warning("[GRAFANA] Correlation check failed: %s", corr_exc)

                            # No correlation found — create a new incident
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
                                (user_id, "grafana", alert_id, alert_title, service,
                                 severity, "investigating", received_at, json.dumps(alert_metadata)),
                            )
                            incident_row = cursor.fetchone()
                            incident_id = incident_row[0] if incident_row else None
                            conn.commit()

                            # Record as the primary alert for this incident
                            try:
                                cursor.execute(
                                    """INSERT INTO incident_alerts
                                       (user_id, incident_id, source_type, source_alert_id, alert_title, alert_service,
                                        alert_severity, correlation_strategy, correlation_score, alert_metadata)
                                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                                    (user_id, incident_id, "grafana", alert_id, alert_title,
                                     service, severity, "primary", 1.0, json.dumps(alert_metadata)),
                                )
                                cursor.execute(
                                    "UPDATE incidents SET affected_services = ARRAY[%s] WHERE id = %s",
                                    (service, incident_id),
                                )
                                conn.commit()
                            except Exception as e:
                                logger.warning("[GRAFANA] Failed to record primary alert: %s", e)

                            if not incident_id:
                                continue

                            logger.info("[GRAFANA][ALERT] Created incident %s for alert %s (fp=%s)", incident_id, alert_id, fingerprint)

                            # Push real-time update to frontend
                            try:
                                from routes.incidents_sse import broadcast_incident_update_to_user_connections
                                broadcast_incident_update_to_user_connections(
                                    user_id, {"type": "incident_update", "incident_id": str(incident_id), "source": "grafana"},
                                )
                            except Exception as e:
                                logger.warning("[GRAFANA][ALERT] Failed to notify SSE: %s", e)

                            # Generate a quick summary (fast, always runs)
                            from chat.background.summarization import generate_incident_summary
                            generate_incident_summary.delay(
                                incident_id=str(incident_id), user_id=user_id, source_type="grafana",
                                alert_title=alert_title or "Unknown Alert", severity=severity,
                                service=service, raw_payload=alert_payload, alert_metadata=alert_metadata,
                            )

                            # Trigger full RCA background chat
                            if _should_trigger_background_chat(user_id, alert_payload):
                                try:
                                    from chat.background.task import (
                                        run_background_chat, create_background_chat_session, is_background_chat_allowed,
                                    )
                                    if not is_background_chat_allowed(user_id):
                                        logger.info("[GRAFANA][ALERT] Skipping background RCA - rate limited for user %s", user_id)
                                    else:
                                        chat_title = f"RCA: {alert_title or 'Grafana Alert'}"
                                        session_id = create_background_chat_session(
                                            user_id=user_id, title=chat_title,
                                            trigger_metadata={"source": "grafana", "alert_uid": alert_uid, "alert_state": alert_state},
                                        )
                                        rca_prompt = _build_rca_prompt_from_alert(alert_payload, user_id=user_id)
                                        task = run_background_chat.delay(
                                            user_id=user_id, session_id=session_id, initial_message=rca_prompt,
                                            trigger_metadata={"source": "grafana", "alert_uid": alert_uid,
                                                              "alert_title": alert_title, "alert_state": alert_state},
                                            incident_id=str(incident_id) if incident_id else None,
                                        )
                                        if incident_id:
                                            cursor.execute(
                                                "UPDATE incidents SET rca_celery_task_id = %s WHERE id = %s",
                                                (task.id, str(incident_id)),
                                            )
                                            conn.commit()
                                        logger.info("[GRAFANA][ALERT] Triggered background RCA for session %s (task_id=%s)", session_id, task.id)
                                except Exception as chat_exc:
                                    logger.exception("[GRAFANA][ALERT] Failed to trigger background chat: %s", chat_exc)

            except Exception as db_exc:
                logger.exception(
                    "[GRAFANA][ALERT] Failed to store alert in database: %s", db_exc
                )
                # Don't raise - we still want to log the alert even if DB insert fails
        else:
            logger.warning(
                "[GRAFANA][ALERT] No user_id provided, alert not stored in database"
            )
    except Exception as exc:  # pragma: no cover - Celery handles retries
        logger.exception("[GRAFANA][ALERT] Failed to process alert payload")
        raise self.retry(exc=exc)
