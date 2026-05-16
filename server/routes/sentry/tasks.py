"""Celery tasks for Sentry webhook processing."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import psycopg2
import requests

from celery_config import celery_app
from chat.background.rca_prompt_builder import build_sentry_rca_prompt
from services.correlation.alert_correlator import AlertCorrelator
from services.correlation import handle_correlated_alert
from utils.payload_timestamp import extract_alert_fired_at

# Exceptions that warrant retrying the task — DB connectivity hiccups,
# transient network errors talking to downstream services. Non-transient
# errors (data validation, type mismatches, programmer errors) are
# re-raised so they fail fast and stay visible.
_TRANSIENT_EXCEPTIONS = (
    psycopg2.OperationalError,
    psycopg2.InterfaceError,
    requests.ConnectionError,
    requests.Timeout,
    ConnectionError,
    TimeoutError,
)

logger = logging.getLogger(__name__)


def extract_sentry_title(payload: Dict[str, Any], resource: str = "") -> str:
    """Extract alert title from a Sentry Integration Platform webhook payload.

    Sentry payload shapes vary by resource:
    - ``issue``: ``data.issue.title``
    - ``error``: ``data.error.title`` (or ``event.title``)
    - ``event_alert``: ``data.event.title`` or ``data.triggered_rule``
    """
    data = payload.get("data") or {}

    issue = data.get("issue") or {}
    if issue.get("title"):
        return str(issue["title"])

    error = data.get("error") or {}
    if error.get("title"):
        return str(error["title"])

    event = data.get("event") or {}
    if event.get("title"):
        return str(event["title"])
    if event.get("message"):
        return str(event["message"])

    triggered_rule = data.get("triggered_rule")
    if triggered_rule:
        return f"Alert triggered: {triggered_rule}"

    if resource:
        return f"Sentry {resource}"
    return "Sentry Alert"


def _extract_level(payload: Dict[str, Any]) -> str:
    """Pull the Sentry level field from any resource shape."""
    data = payload.get("data") or {}
    for candidate in (data.get("issue"), data.get("error"), data.get("event")):
        if isinstance(candidate, dict):
            level = candidate.get("level")
            if level:
                return str(level).lower()
    return ""


def _extract_severity(payload: Dict[str, Any]) -> str:
    """Map Sentry level to Aurora severity bucket."""
    level = _extract_level(payload)
    if level in ("fatal", "error"):
        return "critical"
    if level == "warning":
        return "high"
    if level == "info":
        return "medium"
    if level == "debug":
        return "low"
    return "unknown"


def _extract_project_slug(payload: Dict[str, Any]) -> str:
    """Extract the project slug from the webhook payload."""
    data = payload.get("data") or {}
    for candidate in (data.get("issue"), data.get("error"), data.get("event")):
        if isinstance(candidate, dict):
            project = candidate.get("project") or {}
            if isinstance(project, dict):
                slug = project.get("slug") or project.get("name")
                if slug:
                    return str(slug)[:255]
            project_slug = candidate.get("project_slug")
            if project_slug:
                return str(project_slug)[:255]
    return "unknown"


def _extract_issue_id(payload: Dict[str, Any]) -> Optional[str]:
    """Extract a stable Sentry issue/group identifier."""
    data = payload.get("data") or {}
    for candidate in (data.get("issue"), data.get("error"), data.get("event")):
        if isinstance(candidate, dict):
            iid = candidate.get("id") or candidate.get("issue_id") or candidate.get("groupID")
            if iid:
                return str(iid)
    return None


def _build_alert_metadata(payload: Dict[str, Any], resource: str) -> Dict[str, Any]:
    """Extract Sentry-specific fields for alert_metadata storage."""
    meta: Dict[str, Any] = {"resource": resource, "action": payload.get("action")}
    data = payload.get("data") or {}

    issue = data.get("issue") if isinstance(data.get("issue"), dict) else None
    if issue:
        meta["issueId"] = str(issue.get("id")) if issue.get("id") is not None else None
        meta["issueShortId"] = issue.get("shortId")
        meta["culprit"] = issue.get("culprit")
        meta["level"] = issue.get("level")
        meta["permalink"] = issue.get("permalink") or issue.get("web_url")
        meta["count"] = issue.get("count")
        meta["userCount"] = issue.get("userCount")
        meta["firstSeen"] = issue.get("firstSeen")
        meta["lastSeen"] = issue.get("lastSeen")

    event = data.get("event") if isinstance(data.get("event"), dict) else None
    if event:
        meta.setdefault("eventId", event.get("event_id") or event.get("id"))
        meta.setdefault("culprit", event.get("culprit"))
        meta.setdefault("level", event.get("level"))
        if event.get("environment"):
            meta["environment"] = event.get("environment")
        if event.get("release"):
            meta["release"] = event.get("release")
        tags = event.get("tags")
        if isinstance(tags, list):
            meta["tags"] = tags[:25]

    project_slug = _extract_project_slug(payload)
    if project_slug and project_slug != "unknown":
        meta["projectSlug"] = project_slug

    triggered_rule = data.get("triggered_rule")
    if triggered_rule:
        meta["triggeredRule"] = triggered_rule

    return {k: v for k, v in meta.items() if v is not None}


@celery_app.task(
    bind=True, max_retries=3, default_retry_delay=30, name="sentry.process_event"
)
def process_sentry_event(
    self,
    payload: Dict[str, Any],
    metadata: Optional[Dict[str, Any]] = None,
    user_id: Optional[str] = None,
) -> None:
    """Background processor for Sentry webhook payloads."""
    metadata = metadata or {}
    resource = metadata.get("resource") or ""
    action = payload.get("action") or ""

    title = extract_sentry_title(payload, resource)
    logger.info(
        "[SENTRY][WEBHOOK][USER:%s] %s [%s.%s]",
        user_id or "unknown", title, resource, action,
    )

    try:
        if not user_id:
            logger.warning("[SENTRY][WEBHOOK] Missing user_id; skipping")
            return

        from utils.db.connection_pool import db_pool

        severity = _extract_severity(payload)
        level = _extract_level(payload)
        service = _extract_project_slug(payload)
        issue_id = _extract_issue_id(payload)
        alert_metadata = _build_alert_metadata(payload, resource)

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                from utils.auth.stateless_auth import set_rls_context
                org_id = set_rls_context(cursor, conn, user_id, log_prefix="[SENTRY][WEBHOOK]")
                if not org_id:
                    return

                received_at = datetime.now(timezone.utc)

                cursor.execute(
                    """
                    INSERT INTO sentry_events
                    (user_id, org_id, issue_id, issue_title, level, project_slug,
                     resource, action, payload, received_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (org_id, issue_id, action) WHERE issue_id IS NOT NULL
                    DO UPDATE
                    SET issue_title = EXCLUDED.issue_title,
                        level = EXCLUDED.level,
                        project_slug = EXCLUDED.project_slug,
                        resource = EXCLUDED.resource,
                        payload = EXCLUDED.payload,
                        received_at = EXCLUDED.received_at
                    RETURNING id
                    """,
                    (
                        user_id,
                        org_id,
                        issue_id,
                        title,
                        (level or "")[:50],
                        service[:255] if service else None,
                        (resource or "")[:50],
                        (action or "")[:50],
                        json.dumps(payload),
                        received_at,
                    ),
                )
                event_result = cursor.fetchone()
                event_id = event_result[0] if event_result else None
                conn.commit()

                if not event_id:
                    logger.error("[SENTRY][WEBHOOK] Failed to persist event for user %s", user_id)
                    return

                # source_alert_id is the integer PK of the source-specific event row.
                # Mirrors the pattern used by incidentio/datadog/etc — the FK back to
                # sentry_events.id, not a synthesized string.
                source_alert_id = event_id

                try:
                    correlator = AlertCorrelator()
                    correlation_result = correlator.correlate(
                        cursor=cursor,
                        user_id=user_id,
                        source_type="sentry",
                        source_alert_id=source_alert_id,
                        alert_title=title,
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
                            source_type="sentry",
                            source_alert_id=source_alert_id,
                            alert_title=title,
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
                        "[SENTRY] Correlation check failed, proceeding with new incident: %s",
                        corr_exc,
                    )

                alert_fired_at = extract_alert_fired_at(
                    payload,
                    [
                        "data.issue.firstSeen",
                        "data.issue.lastSeen",
                        "data.event.received",
                        "data.event.timestamp",
                        "data.error.firstSeen",
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
                        "sentry",
                        source_alert_id,
                        title,
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
                            "sentry",
                            source_alert_id,
                            title,
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
                    logger.warning("[SENTRY] Failed to record primary alert: %s", e)

                if incident_id:
                    logger.info(
                        "[SENTRY][WEBHOOK] Created incident %s (alert=%s)",
                        incident_id, source_alert_id,
                    )

                    try:
                        from routes.incidents_sse import broadcast_incident_update_to_user_connections
                        broadcast_incident_update_to_user_connections(
                            user_id,
                            {"type": "incident_update", "incident_id": str(incident_id), "source": "sentry"},
                            org_id=org_id,
                        )
                    except Exception as e:
                        logger.warning("[SENTRY][WEBHOOK] Failed to notify SSE: %s", e)

                    from chat.background.summarization import generate_incident_summary
                    generate_incident_summary.delay(
                        incident_id=str(incident_id),
                        user_id=user_id,
                        source_type="sentry",
                        alert_title=title or "Sentry Alert",
                        severity=severity,
                        service=service,
                        raw_payload=payload,
                        alert_metadata=alert_metadata,
                    )

                    try:
                        from chat.background.task import (
                            run_background_chat,
                            create_background_chat_session,
                            is_background_chat_allowed,
                        )

                        if not is_background_chat_allowed(user_id):
                            logger.info("[SENTRY][WEBHOOK] Skipping background RCA — rate limited for user %s", user_id)
                        else:
                            session_id = create_background_chat_session(
                                user_id=user_id,
                                title=f"RCA: {title}",
                                trigger_metadata={
                                    "source": "sentry",
                                    "resource": resource,
                                    "action": action,
                                    "issueId": issue_id,
                                },
                                incident_id=str(incident_id),
                            )

                            rca_prompt, rail_text = build_sentry_rca_prompt(payload, user_id=user_id)

                            task = run_background_chat.delay(
                                user_id=user_id,
                                session_id=session_id,
                                initial_message=rca_prompt,
                                trigger_metadata={
                                    "source": "sentry",
                                    "resource": resource,
                                    "action": action,
                                    "issueId": issue_id,
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
                                "[SENTRY][WEBHOOK] Triggered background RCA for session %s (task_id=%s)",
                                session_id, task.id,
                            )
                    except Exception:
                        logger.exception("[SENTRY][WEBHOOK] Failed to trigger RCA")

    except _TRANSIENT_EXCEPTIONS as exc:
        logger.exception("[SENTRY][WEBHOOK] Transient failure; retrying")
        raise self.retry(exc=exc)
    except Exception:
        logger.exception("[SENTRY][WEBHOOK] Non-transient failure; not retrying")
        raise
