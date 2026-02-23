"""Celery tasks for Dynatrace integrations."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from celery_config import celery_app
from services.correlation.alert_correlator import AlertCorrelator
from services.correlation import handle_correlated_alert
from utils.auth.stateless_auth import get_user_preference

logger = logging.getLogger(__name__)

_SEVERITY_MAP = {
    "AVAILABILITY": "critical",
    "ERROR": "high",
    "PERFORMANCE": "medium",
    "RESOURCE_CONTENTION": "medium",
    "CUSTOM_ALERT": "low",
}


def _extract_severity(payload: dict[str, Any]) -> str:
    return _SEVERITY_MAP.get(str(payload.get("ProblemSeverity", "")).upper(), "unknown")


def _extract_service(payload: dict[str, Any]) -> str:
    return str(payload.get("ImpactedEntity") or payload.get("Tags") or "unknown")[:255]


def _should_trigger_rca(user_id: str) -> bool:
    return get_user_preference(user_id, "dynatrace_rca_enabled", default=False)


def _build_rca_prompt(payload: dict[str, Any], user_id: str | None = None) -> str:
    title = payload.get("ProblemTitle") or "Unknown Problem"
    parts = [
        "A Dynatrace problem has been detected and requires Root Cause Analysis.",
        "",
        "PROBLEM DETAILS:",
        f"- Title: {title}",
        f"- Severity: {payload.get('ProblemSeverity') or 'unknown'}",
        f"- Impact: {payload.get('ProblemImpact') or 'unknown'}",
        f"- Impacted Entity: {payload.get('ImpactedEntity') or 'unknown'}",
    ]
    if url := payload.get("ProblemURL"):
        parts.append(f"- Problem URL: {url}")
    if tags := payload.get("Tags"):
        parts.append(f"- Tags: {tags}")

    try:
        from chat.background.rca_prompt_builder import inject_aurora_learn_context
        inject_aurora_learn_context(parts, user_id, title, _extract_service(payload), "dynatrace")
    except Exception as exc:
        logger.warning("[AURORA LEARN] Failed to get context: %s", exc)

    return "\n".join(parts)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30, name="dynatrace.process_problem")
def process_dynatrace_problem(
    self,
    payload: dict[str, Any],
    metadata: dict[str, Any] | None = None,
    user_id: str | None = None,
) -> None:
    """Background processor for Dynatrace problem notification webhooks."""
    title = payload.get("ProblemTitle") or "Unknown Problem"

    try:
        if not user_id:
            logger.warning("[DYNATRACE][ALERT] No user_id provided, skipping")
            return

        logger.info("[DYNATRACE][ALERT][USER:%s] %s", user_id, title)

        from utils.db.connection_pool import db_pool

        severity = _extract_severity(payload)
        service = _extract_service(payload)
        received_at = datetime.now(timezone.utc)
        alert_metadata = {
            k: v for k, v in {
                "problemId": payload.get("ProblemID"),
                "problemUrl": payload.get("ProblemURL"),
                "impact": payload.get("ProblemImpact"),
                "tags": payload.get("Tags"),
            }.items() if v
        }

        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()

            cursor.execute(
                """INSERT INTO dynatrace_problems
                   (user_id, problem_id, pid, problem_title, problem_state, severity,
                    impact, impacted_entity, problem_url, tags, payload, received_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                (
                    user_id, payload.get("ProblemID"), payload.get("PID"), title,
                    payload.get("State", "OPEN"), severity, payload.get("ProblemImpact"),
                    payload.get("ImpactedEntity"), payload.get("ProblemURL"),
                    payload.get("Tags"), json.dumps(payload), received_at,
                ),
            )
            alert_db_id = cursor.fetchone()[0]
            conn.commit()

            try:
                correlator = AlertCorrelator()
                result = correlator.correlate(
                    cursor=cursor, user_id=user_id, source_type="dynatrace",
                    source_alert_id=alert_db_id, alert_title=title,
                    alert_service=service, alert_severity=severity,
                    alert_metadata=alert_metadata,
                )
                if result.is_correlated:
                    handle_correlated_alert(
                        cursor=cursor, user_id=user_id, incident_id=result.incident_id,
                        source_type="dynatrace", source_alert_id=alert_db_id,
                        alert_title=title, alert_service=service, alert_severity=severity,
                        correlation_result=result, alert_metadata=alert_metadata,
                        raw_payload=payload,
                    )
                    conn.commit()
                    return
            except Exception as corr_exc:
                logger.warning("[DYNATRACE] Correlation failed, proceeding: %s", corr_exc)

            if not _should_trigger_rca(user_id):
                conn.commit()
                logger.info("[DYNATRACE][ALERT] Stored for user %s (RCA disabled)", user_id)
                return

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
                (user_id, "dynatrace", alert_db_id, title, service,
                 severity, "investigating", received_at, json.dumps(alert_metadata)),
            )
            incident_row = cursor.fetchone()
            incident_id = incident_row[0] if incident_row else None

            if incident_id:
                cursor.execute(
                    """INSERT INTO incident_alerts
                       (user_id, incident_id, source_type, source_alert_id, alert_title,
                        alert_service, alert_severity, correlation_strategy, correlation_score, alert_metadata)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (user_id, incident_id, "dynatrace", alert_db_id, title,
                     service, severity, "primary", 1.0, json.dumps(alert_metadata)),
                )
                cursor.execute(
                    "UPDATE incidents SET affected_services = ARRAY[%s] WHERE id = %s",
                    (service, incident_id),
                )
            conn.commit()

        if not incident_id:
            return

        logger.info("[DYNATRACE][ALERT] Created incident %s for problem %s", incident_id, alert_db_id)

        from chat.background.summarization import generate_incident_summary
        generate_incident_summary.delay(
            incident_id=str(incident_id), user_id=user_id, source_type="dynatrace",
            alert_title=title, severity=severity, service=service,
            raw_payload=payload, alert_metadata=alert_metadata,
        )

        try:
            from chat.background.task import (
                run_background_chat, create_background_chat_session, is_background_chat_allowed,
            )
            if not is_background_chat_allowed(user_id):
                logger.info("[DYNATRACE] Skipping RCA - rate limited for user %s", user_id)
                return

            session_id = create_background_chat_session(
                user_id=user_id,
                title=f"RCA: {title}",
                trigger_metadata={"source": "dynatrace", "problem_id": payload.get("ProblemID")},
            )
            task = run_background_chat.delay(
                user_id=user_id, session_id=session_id,
                initial_message=_build_rca_prompt(payload, user_id=user_id),
                trigger_metadata={"source": "dynatrace", "problem_id": payload.get("ProblemID")},
                incident_id=str(incident_id),
            )
            with db_pool.get_admin_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE incidents SET rca_celery_task_id = %s WHERE id = %s",
                    (task.id, str(incident_id)),
                )
                conn.commit()
            logger.info("[DYNATRACE] Triggered RCA for session %s (task=%s)", session_id, task.id)
        except Exception as chat_exc:
            logger.exception("[DYNATRACE] Failed to trigger background chat: %s", chat_exc)

    except Exception as exc:
        logger.exception("[DYNATRACE][ALERT] Failed to process problem '%s' for user %s", title, user_id)
        raise self.retry(exc=exc)
