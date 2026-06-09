"""Background Celery task for Jira webhook processing.

Handles Jira issue webhooks (issue_created, issue_updated) and triggers
Aurora's RCA pipeline — the same flow as Datadog/Grafana/PagerDuty alerts.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from celery_config import celery_app
from chat.background.rca_prompt_builder import build_rca_prompt
from services.correlation.alert_correlator import AlertCorrelator
from services.correlation import handle_correlated_alert
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)


def _extract_severity(issue: Dict[str, Any]) -> str:
    priority = (issue.get("fields", {}).get("priority") or {}).get("name", "").lower()
    if priority in ("highest", "blocker", "critical"):
        return "critical"
    elif priority in ("high", "major"):
        return "high"
    elif priority in ("medium", "normal"):
        return "medium"
    return "low"


def _extract_service(issue: Dict[str, Any]) -> str:
    fields = issue.get("fields", {})
    components = fields.get("components") or []
    if components:
        return components[0].get("name", "unknown")
    labels = fields.get("labels") or []
    for label in labels:
        if label.startswith("service:"):
            return label.split(":", 1)[1]
    project = fields.get("project", {}).get("name", "")
    return project or "unknown"


def _build_alert_metadata(issue: Dict[str, Any], webhook_event: str) -> Dict[str, Any]:
    fields = issue.get("fields", {})
    meta: Dict[str, Any] = {
        "issueKey": issue.get("key", ""),
        "issueType": (fields.get("issuetype") or {}).get("name", ""),
        "priority": (fields.get("priority") or {}).get("name", ""),
        "status": (fields.get("status") or {}).get("name", ""),
        "webhookEvent": webhook_event,
    }
    if fields.get("assignee"):
        meta["assignee"] = fields["assignee"].get("displayName", "")
    reporter = fields.get("reporter") or {}
    if reporter:
        meta["reporter"] = reporter.get("displayName", "")
    labels = fields.get("labels")
    if labels:
        meta["labels"] = labels
    components = fields.get("components")
    if components:
        meta["components"] = [c.get("name") for c in components]
    if fields.get("description"):
        desc = fields["description"]
        if isinstance(desc, str):
            meta["description"] = desc[:2000]
        elif isinstance(desc, dict):
            meta["description"] = json.dumps(desc)[:2000]
    return meta


@celery_app.task(
    bind=True, max_retries=3, default_retry_delay=30, name="jira.process_webhook"
)
def process_jira_webhook(
    self,
    payload: Dict[str, Any],
    user_id: Optional[str] = None,
) -> None:
    """Process a Jira webhook payload: create incident and trigger RCA."""
    webhook_event = payload.get("webhookEvent", "")
    issue = payload.get("issue", {})
    issue_key = issue.get("key", "unknown")
    fields = issue.get("fields", {})
    summary = fields.get("summary", "")

    logger.info("[JIRA][WEBHOOK][USER:%s] Processing webhook event",
                user_id or "unknown")

    try:
        if not user_id:
            logger.warning("[JIRA][WEBHOOK] Missing user_id; skipping")
            return

        if not issue:
            logger.warning("[JIRA][WEBHOOK] No issue in payload; skipping")
            return

        from utils.db.connection_pool import db_pool

        severity = _extract_severity(issue)
        service = _extract_service(issue)
        alert_title = f"[{issue_key}] {summary}"
        alert_metadata = _build_alert_metadata(issue, webhook_event)

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                from utils.auth.stateless_auth import set_rls_context
                org_id = set_rls_context(cursor, conn, user_id, log_prefix="[JIRA][WEBHOOK]")
                if not org_id:
                    return

                received_at = datetime.now(timezone.utc)

                alert_fired_at = None
                created_str = fields.get("created")
                if created_str:
                    try:
                        alert_fired_at = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                    except (ValueError, TypeError):
                        logger.debug("[JIRA] Could not parse created date: %s", created_str)

                # source_alert_id in incidents is an integer — stable hash of issue key
                issue_id_int = int(hashlib.sha256(f"{org_id}:{issue_key}".encode()).hexdigest()[:8], 16) % (2**31)

                try:
                    correlator = AlertCorrelator()
                    correlation_result = correlator.correlate(
                        cursor=cursor,
                        user_id=user_id,
                        source_type="jira",
                        source_alert_id=issue_id_int,
                        alert_title=alert_title,
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
                            source_type="jira",
                            source_alert_id=issue_id_int,
                            alert_title=alert_title,
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
                    logger.warning("[JIRA] Correlation check failed: %s", corr_exc)

                cursor.execute(
                    """
                    INSERT INTO incidents
                    (user_id, org_id, source_type, source_alert_id, alert_title, alert_service,
                     severity, status, started_at, alert_metadata, alert_fired_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (org_id, source_type, source_alert_id, user_id) DO UPDATE
                    SET updated_at = CURRENT_TIMESTAMP,
                        alert_metadata = EXCLUDED.alert_metadata
                    RETURNING id, (xmax = 0) AS inserted
                    """,
                    (
                        user_id,
                        org_id,
                        "jira",
                        issue_id_int,
                        alert_title,
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
                incident_was_inserted = bool(incident_row[1]) if incident_row else False
                conn.commit()

                if not incident_id:
                    logger.error("[JIRA][WEBHOOK] Failed to create incident for %s", sanitize(issue_key))
                    return

                try:
                    cursor.execute(
                        """INSERT INTO incident_alerts
                           (user_id, org_id, incident_id, source_type, source_alert_id, alert_title,
                            alert_service, alert_severity, correlation_strategy, correlation_score, alert_metadata)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (
                            user_id, org_id, incident_id, "jira", issue_id_int,
                            alert_title, service, severity, "primary", 1.0,
                            json.dumps(alert_metadata),
                        ),
                    )
                    cursor.execute(
                        "UPDATE incidents SET affected_services = ARRAY[%s] WHERE id = %s",
                        (service, incident_id),
                    )
                    conn.commit()
                except Exception as e:
                    logger.warning("[JIRA] Failed to record primary alert: %s", e)

                if incident_id and incident_was_inserted:
                    try:
                        cursor.execute("SAVEPOINT sp_jira_lifecycle")
                        cursor.execute(
                            """INSERT INTO incident_lifecycle_events
                               (incident_id, user_id, org_id, event_type, new_value)
                               VALUES (%s, %s, %s, %s, %s)""",
                            (incident_id, user_id, org_id, 'created', 'investigating'),
                        )
                        cursor.execute("RELEASE SAVEPOINT sp_jira_lifecycle")
                        conn.commit()
                    except Exception as e:
                        try:
                            cursor.execute("ROLLBACK TO SAVEPOINT sp_jira_lifecycle")
                        except Exception as rb_exc:
                            logger.debug("[JIRA] Savepoint rollback failed: %s", rb_exc)
                        logger.warning("[JIRA] Failed to record lifecycle event: %s", e)

                if incident_id:
                    logger.info("[JIRA][WEBHOOK] Created incident %s for %s", incident_id, sanitize(issue_key))

                    try:
                        from routes.incidents_sse import broadcast_incident_update_to_user_connections
                        broadcast_incident_update_to_user_connections(
                            user_id,
                            {"type": "incident_update", "incident_id": str(incident_id), "source": "jira"},
                            org_id=org_id,
                        )
                    except Exception as e:
                        logger.warning("[JIRA][WEBHOOK] Failed to notify SSE: %s", e)

                    from chat.background.summarization import generate_incident_summary
                    generate_incident_summary.delay(
                        incident_id=str(incident_id),
                        user_id=user_id,
                        source_type="jira",
                        alert_title=alert_title,
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
                            logger.info("[JIRA][WEBHOOK] Skipping RCA - rate limited for user %s", user_id)
                        else:
                            session_id = create_background_chat_session(
                                user_id=user_id,
                                title=f"RCA: {alert_title}",
                                trigger_metadata={
                                    "source": "jira",
                                    "issue_key": issue_key,
                                },
                                incident_id=str(incident_id),
                            )

                            rca_prompt, rail_text = build_rca_prompt(
                                "jira", alert_title, payload, user_id=user_id
                            )

                            task = run_background_chat.delay(
                                user_id=user_id,
                                session_id=session_id,
                                initial_message=rca_prompt,
                                trigger_metadata={
                                    "source": "jira",
                                    "issue_key": issue_key,
                                },
                                incident_id=str(incident_id),
                                rail_text=rail_text,
                            )

                            cursor.execute(
                                "UPDATE incidents SET rca_celery_task_id = %s WHERE id = %s",
                                (task.id, str(incident_id))
                            )
                            conn.commit()

                            logger.info(
                                "[JIRA][WEBHOOK] Triggered RCA for %s session=%s task=%s",
                                sanitize(issue_key), session_id, task.id,
                            )
                    except Exception as e:
                        logger.error("[JIRA][WEBHOOK] Failed to trigger RCA: %s", e)

    except Exception as exc:
        logger.exception("[JIRA][WEBHOOK] Failed to process webhook")
        raise self.retry(exc=exc)
