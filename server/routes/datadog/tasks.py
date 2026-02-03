"""Celery tasks for Datadog integrations."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from celery_config import celery_app
from services.correlation.alert_correlator import AlertCorrelator

logger = logging.getLogger(__name__)


def _summarize_event(payload: Dict[str, Any]) -> str:
    title = (
        payload.get("title")
        or payload.get("event_title")
        or payload.get("event", {}).get("title")
        or "Datadog Event"
    )
    event_type = (
        payload.get("event_type")
        or payload.get("alert_type")
        or payload.get("alert_transition", {}).get("new_status")
    )
    status = payload.get("status") or payload.get("alert_type") or payload.get("state")
    monitor_id = payload.get("monitor_id") or payload.get("alert_id")
    parts = [title]
    if event_type:
        parts.append(f"[{event_type}]")
    if status:
        parts.append(f"status={status}")
    if monitor_id:
        parts.append(f"monitor={monitor_id}")
    return " ".join(parts)


def _safe_json_dump(data: Dict[str, Any]) -> str:
    try:
        return json.dumps(data, ensure_ascii=False)
    except Exception:  # pragma: no cover - defensive
        return str(data)


def _should_trigger_background_chat(user_id: str, payload: Dict[str, Any]) -> bool:
    """Determine if a background chat should be triggered for this alert.

    Args:
        user_id: The user ID receiving the alert
        payload: The Datadog alert payload

    Returns:
        True if a background chat should be triggered
    """
    # Check user preference for automated RCA
    # from utils.auth.stateless_auth import get_user_preference
    # rca_enabled = get_user_preference(user_id, "automated_rca_enabled", default=False)
    #
    # if not rca_enabled:
    #     logger.debug("[DATADOG] Skipping background RCA - disabled in user preferences for user %s", user_id)
    #     return False

    # Always trigger RCA for any webhook received
    return True


def _build_rca_prompt_from_alert(
    payload: Dict[str, Any], user_id: Optional[str] = None
) -> str:
    """Build an RCA analysis prompt from a Datadog alert payload.

    Args:
        payload: The Datadog alert payload
        user_id: Optional user ID for Aurora Learn context injection

    Returns:
        A prompt string for the background chat agent
    """
    title = (
        payload.get("title")
        or payload.get("event_title")
        or payload.get("event", {}).get("title")
        or "Unknown Alert"
    )
    status = (
        payload.get("status")
        or payload.get("state")
        or payload.get("alert_type")
        or "unknown"
    )
    event_type = payload.get("event_type") or payload.get("alert_type") or "unknown"

    # Extract scope/tags for context
    scope = payload.get("scope") or payload.get("event", {}).get("scope") or "none"
    tags = payload.get("tags", [])
    tags_str = ", ".join(tags[:10]) if tags else "none"  # Limit to 10 tags

    # Extract monitor info
    monitor_id = payload.get("monitor_id") or payload.get("alert_id") or "unknown"
    monitor_name = payload.get("monitor_name") or title

    # Extract message/description
    message = (
        payload.get("body")
        or payload.get("message")
        or payload.get("event", {}).get("text")
        or ""
    )

    # Build the prompt parts
    prompt_parts = [
        "A Datadog alert has been triggered and requires Root Cause Analysis.",
        "",
        "ALERT DETAILS:",
        f"- Title: {title}",
        f"- Monitor: {monitor_name} (ID: {monitor_id})",
        f"- Status: {status}",
        f"- Event Type: {event_type}",
        f"- Scope: {scope}",
        f"- Tags: {tags_str}",
    ]

    if message:
        prompt_parts.append(f"- Message: {message}")

    # Add Aurora Learn context if available
    try:
        from chat.background.rca_prompt_builder import inject_aurora_learn_context

        # Extract service from tags if available
        service = next(
            (tag.split(":", 1)[1] for tag in tags if tag.startswith("service:")), ""
        )
        inject_aurora_learn_context(prompt_parts, user_id, title, service, "datadog")
    except Exception as e:
        logger.warning(f"[AURORA LEARN] Failed to get context: {e}")

    return "\n".join(prompt_parts)


def _extract_severity(payload: Dict[str, Any]) -> str:
    """Extract severity from Datadog event payload.

    Datadog uses $ALERT_TYPE for severity indication:
    - error: critical severity
    - warning: high severity
    - info: low severity
    - success: low severity
    """
    alert_type = payload.get("alert_type", "").lower()
    if alert_type == "error":
        return "critical"
    elif alert_type == "warning":
        return "high"
    elif alert_type in ("info", "success"):
        return "low"

    return "unknown"


def _extract_service(payload: Dict[str, Any]) -> str:
    """Extract service name from Datadog payload."""
    # Try various fields
    tags = payload.get("tags", [])

    # Handle tags as string (comma-separated) or list
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]

    for tag in tags:
        if isinstance(tag, str) and tag.startswith("service:"):
            return tag.split(":", 1)[1][:255]

    # Fallback to hostname or service field, but NOT title (to avoid duplication)
    service = payload.get("hostname") or payload.get("host") or payload.get("service")
    return str(service)[:255] if service else "unknown"


@celery_app.task(
    bind=True, max_retries=3, default_retry_delay=30, name="datadog.process_event"
)
def process_datadog_event(
    self,
    payload: Dict[str, Any],
    metadata: Optional[Dict[str, Any]] = None,
    user_id: Optional[str] = None,
) -> None:
    """Background processor for Datadog webhook payloads."""
    received_at = datetime.now(timezone.utc)
    summary = _summarize_event(payload)
    logger.info("[DATADOG][WEBHOOK][USER:%s] %s", user_id or "unknown", summary)
    logger.debug("[DATADOG][WEBHOOK] payload=%s", _safe_json_dump(payload))

    try:
        if not user_id:
            logger.warning("[DATADOG][WEBHOOK] Missing user_id; skipping persistence")
            return

        from utils.db.connection_pool import db_pool

        event_type = payload.get("event_type") or payload.get("alert_type")
        event_title = (
            payload.get("title")
            or payload.get("event_title")
            or payload.get("event", {}).get("title")
        )
        status = (
            payload.get("status") or payload.get("state") or payload.get("alert_type")
        )
        scope = payload.get("scope") or payload.get("event", {}).get("scope")

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO datadog_events (user_id, event_type, event_title, status, scope, payload, received_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        user_id,
                        event_type,
                        event_title,
                        status,
                        scope,
                        json.dumps(payload),
                        received_at,
                    ),
                )
                event_result = cursor.fetchone()
                event_id = event_result[0] if event_result else None
                conn.commit()

                if not event_id:
                    logger.error(
                        "[DATADOG][WEBHOOK] Failed to get event_id for user %s", user_id
                    )
                    return

                logger.info("[DATADOG][WEBHOOK] Stored event for user %s", user_id)

                # Create incident record
                severity = _extract_severity(payload)
                service = _extract_service(payload)

                # Build alert metadata with Datadog-specific fields
                alert_metadata = {}
                if payload.get("alert_id") or payload.get("id"):
                    alert_metadata["alertId"] = str(
                        payload.get("alert_id") or payload.get("id")
                    )
                if payload.get("metric"):
                    alert_metadata["metric"] = payload.get("metric")
                if payload.get("query"):
                    alert_metadata["query"] = payload.get("query")
                hostname = payload.get("hostname") or payload.get("host")
                if hostname:
                    alert_metadata["hostname"] = hostname
                tags = payload.get("tags") or payload.get("scope")
                if tags:
                    alert_metadata["tags"] = tags
                if payload.get("body") or payload.get("message"):
                    alert_metadata["message"] = payload.get("body") or payload.get(
                        "message"
                    )
                if payload.get("link") or payload.get("url"):
                    alert_metadata["alertUrl"] = payload.get("link") or payload.get(
                        "url"
                    )
                if payload.get("priority"):
                    alert_metadata["priority"] = payload.get("priority")
                if payload.get("snapshot"):
                    alert_metadata["snapshotUrl"] = payload.get("snapshot")

                try:
                    correlator = AlertCorrelator()
                    correlation_result = correlator.correlate(
                        cursor=cursor,
                        user_id=user_id,
                        source_type="datadog",
                        source_alert_id=event_id,
                        alert_title=event_title,
                        alert_service=service,
                        alert_severity=severity,
                        alert_metadata=alert_metadata,
                    )

                    if correlation_result.is_correlated:
                        incident_id = correlation_result.incident_id
                        cursor.execute(
                            """INSERT INTO incident_alerts
                               (incident_id, source_type, source_alert_id, alert_title, alert_service,
                                alert_severity, correlation_strategy, correlation_score,
                                correlation_details, alert_metadata)
                               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                            (
                                incident_id,
                                "datadog",
                                event_id,
                                event_title,
                                service,
                                severity,
                                correlation_result.strategy,
                                correlation_result.score,
                                json.dumps(correlation_result.details),
                                json.dumps(alert_metadata),
                            ),
                        )
                        cursor.execute(
                            """UPDATE incidents
                               SET correlated_alert_count = correlated_alert_count + 1,
                                   affected_services = CASE
                                       WHEN affected_services IS NULL THEN ARRAY[%(service)s]
                                       WHEN NOT (%(service)s = ANY(affected_services)) THEN array_append(affected_services, %(service)s)
                                       ELSE affected_services
                                   END,
                                   updated_at = CURRENT_TIMESTAMP
                               WHERE id = %(incident_id)s""",
                            {"service": service, "incident_id": incident_id},
                        )
                        conn.commit()

                        try:
                            from routes.incidents_sse import (
                                broadcast_incident_update_to_user_connections,
                            )

                            broadcast_incident_update_to_user_connections(
                                user_id,
                                {
                                    "type": "alert_correlated",
                                    "incident_id": str(incident_id),
                                    "source": "datadog",
                                    "alert_title": event_title,
                                    "correlation_score": correlation_result.score,
                                },
                            )
                        except Exception as e:
                            logger.warning("[DATADOG] Failed to notify SSE: %s", e)

                        logger.info(
                            "[DATADOG] Alert correlated to incident %s (score=%.2f, strategy=%s)",
                            incident_id,
                            correlation_result.score,
                            correlation_result.strategy,
                        )
                        return
                except Exception as corr_exc:
                    logger.warning(
                        "[DATADOG] Correlation check failed, proceeding with normal flow: %s",
                        corr_exc,
                    )

                cursor.execute(
                    """
                    INSERT INTO incidents 
                    (user_id, source_type, source_alert_id, alert_title, alert_service, 
                     severity, status, started_at, alert_metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (source_type, source_alert_id, user_id) DO UPDATE
                    SET updated_at = CURRENT_TIMESTAMP,
                        started_at = CASE 
                            WHEN incidents.status != 'analyzed' THEN EXCLUDED.started_at
                            ELSE incidents.started_at
                        END,
                        alert_metadata = EXCLUDED.alert_metadata
                    RETURNING id
                    """,
                    (
                        user_id,
                        "datadog",
                        event_id,
                        event_title,
                        service,
                        severity,
                        "investigating",
                        received_at,
                        json.dumps(alert_metadata),
                    ),
                )
                incident_row = cursor.fetchone()
                incident_id = incident_row[0] if incident_row else None
                conn.commit()

                try:
                    cursor.execute(
                        """INSERT INTO incident_alerts
                           (incident_id, source_type, source_alert_id, alert_title, alert_service,
                            alert_severity, correlation_strategy, correlation_score, alert_metadata)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (
                            incident_id,
                            "datadog",
                            event_id,
                            event_title,
                            service,
                            severity,
                            "primary",
                            1.0,
                            json.dumps(alert_metadata),
                        ),
                    )
                    cursor.execute(
                        "UPDATE incidents SET affected_services = ARRAY[%s] WHERE id = %s AND (affected_services IS NULL OR affected_services = '{}')",
                        (service, incident_id),
                    )
                    conn.commit()
                except Exception as e:
                    logger.warning("[DATADOG] Failed to record primary alert: %s", e)

                if incident_id:
                    logger.info(
                        "[DATADOG][WEBHOOK] Created incident %s for event %s",
                        incident_id,
                        event_id,
                    )

                    # Notify SSE connections about incident update
                    try:
                        from routes.incidents_sse import (
                            broadcast_incident_update_to_user_connections,
                        )

                        broadcast_incident_update_to_user_connections(
                            user_id,
                            {
                                "type": "incident_update",
                                "incident_id": str(incident_id),
                                "source": "datadog",
                            },
                        )
                    except Exception as e:
                        logger.warning(f"[DATADOG][WEBHOOK] Failed to notify SSE: {e}")

                    # Trigger summary generation
                    from chat.background.summarization import generate_incident_summary

                    generate_incident_summary.delay(
                        incident_id=str(incident_id),
                        user_id=user_id,
                        source_type="datadog",
                        alert_title=event_title or "Unknown Event",
                        severity=severity,
                        service=service,
                        raw_payload=payload,
                        alert_metadata=alert_metadata,
                    )

                    # Trigger background chat for RCA if enabled
                    if _should_trigger_background_chat(user_id, payload):
                        try:
                            from chat.background.task import (
                                run_background_chat,
                                create_background_chat_session,
                                is_background_chat_allowed,
                            )

                            if not is_background_chat_allowed(user_id):
                                logger.info(
                                    "[DATADOG][WEBHOOK] Skipping background RCA - rate limited for user %s",
                                    user_id,
                                )
                            else:
                                session_id = create_background_chat_session(
                                    user_id=user_id,
                                    title=f"RCA: {event_title or 'Datadog Alert'}",
                                    trigger_metadata={
                                        "source": "datadog",
                                        "monitor_id": payload.get("monitor_id")
                                        or payload.get("alert_id"),
                                        "status": status,
                                    },
                                )

                                # Build simple RCA prompt with Aurora Learn context injection
                                rca_prompt = _build_rca_prompt_from_alert(
                                    payload, user_id=user_id
                                )

                                run_background_chat.delay(
                                    user_id=user_id,
                                    session_id=session_id,
                                    initial_message=rca_prompt,
                                    trigger_metadata={
                                        "source": "datadog",
                                        "monitor_id": payload.get("monitor_id")
                                        or payload.get("alert_id"),
                                        "status": status,
                                    },
                                    incident_id=str(incident_id),
                                )
                                logger.info(
                                    "[DATADOG][WEBHOOK] Triggered background RCA for session %s",
                                    session_id,
                                )
                        except Exception as e:
                            logger.error(
                                "[DATADOG][WEBHOOK] Failed to trigger RCA: %s", e
                            )

    except Exception as exc:
        logger.exception("[DATADOG][WEBHOOK] Failed to process webhook payload")
        raise self.retry(exc=exc)
