"""Celery task: turn a stored pipeline failure into an incident + background RCA.

Mirrors ``routes/jenkins/tasks.py`` with three deliberate differences:

* no correlation: a pipeline submission never attaches to (and so never
  wakes the outbound channels of) another source's open incident;
* ``send_notifications=False`` and ``source="pipeline"`` on the RCA, which
  together silence start/complete/failed notifications, on_incident actions,
  Jira filing and outbound-write agent tools (see chat/background/task.py
  and cloud_tools.py);
* the prompt carries the deterministic triage so the agent starts from the
  evidence instead of rediscovering it.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict

from celery_config import celery_app
from chat.background.rca_prompt_builder import build_rca_prompt
from services.pipeline_intake.evidence import PipelineFailureEvidence
from services.pipeline_intake.triage import rail_text, rca_payload, render_prompt_section

logger = logging.getLogger(__name__)

SOURCE = "pipeline"

_SEVERITY = {
    "platform": "high",
    "ingestion_upstream": "high",
    "execution": "high",
    "schema_drift": "high",
    "data_test": "medium",
    "unknown": "medium",
}


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30, name="pipeline.process_failure")
def process_pipeline_failure(self, event_id: int, user_id: str) -> None:
    log_prefix = "[PIPELINE][TASK]"
    try:
        from utils.auth.stateless_auth import set_rls_context
        from utils.db.connection_pool import db_pool

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                org_id = set_rls_context(cursor, conn, user_id, log_prefix=log_prefix)
                if not org_id:
                    return
                cursor.execute("SELECT payload FROM pipeline_failure_events WHERE id = %s", (event_id,))
                row = cursor.fetchone()
                if not row:
                    logger.warning("%s event %s not found", log_prefix, event_id)
                    return
                stored: Dict[str, Any] = row[0] if isinstance(row[0], dict) else json.loads(row[0])
                evidence = PipelineFailureEvidence.from_dict(stored["evidence"])
                tri = stored["triage"]
                history = stored.get("history") or {}

                received_at = datetime.now(timezone.utc)
                title = evidence.title or f"Pipeline failure in {evidence.pipeline_name}"
                service = (evidence.pipeline_name or "pipeline")[:255]
                severity = _SEVERITY.get(tri.get("category"), "medium")
                alert_metadata = {
                    "pipeline": evidence.pipeline_name,
                    "sourceKind": evidence.source_kind,
                    "runId": evidence.run_id,
                    "category": tri.get("category"),
                    "categoryConfidence": tri.get("confidence"),
                    "ruleId": tri.get("rule_id"),
                    "primaryNode": tri.get("primary_node"),
                    "evidenceLines": tri.get("evidence_lines"),
                    "pipelineEventId": event_id,
                }

                cursor.execute(
                    """INSERT INTO incidents
                       (user_id, org_id, source_type, source_alert_id, alert_title, alert_service,
                        severity, status, started_at, alert_metadata)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (org_id, source_type, source_alert_id, user_id) DO UPDATE
                       SET updated_at = CURRENT_TIMESTAMP
                       RETURNING id, (xmax = 0) AS inserted""",
                    (user_id, org_id, SOURCE, event_id, title, service, severity,
                     "investigating", received_at, json.dumps(alert_metadata, default=str)),
                )
                inc_row = cursor.fetchone()
                incident_id, inserted = (inc_row[0], bool(inc_row[1])) if inc_row else (None, False)
                if not incident_id or not inserted:
                    # Celery retry after the incident was created: the RCA is already queued.
                    conn.commit()
                    return
                cursor.execute(
                    """INSERT INTO incident_alerts
                       (user_id, org_id, incident_id, source_type, source_alert_id, alert_title,
                        alert_service, alert_severity, correlation_strategy, correlation_score, alert_metadata)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (user_id, org_id, incident_id, SOURCE, event_id, title, service, severity,
                     "primary", 1.0, json.dumps(alert_metadata, default=str)),
                )
                cursor.execute(
                    "UPDATE incidents SET affected_services = ARRAY[%s] WHERE id = %s",
                    (service, incident_id),
                )
                cursor.execute(
                    """INSERT INTO incident_lifecycle_events
                       (incident_id, user_id, org_id, event_type, new_value)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (incident_id, user_id, org_id, "created", "investigating"),
                )
                conn.commit()
                logger.info("%s created incident %s for event %s", log_prefix, incident_id, event_id)

                # In-app only: refresh the incidents list for this user's open tabs.
                try:
                    from routes.incidents_sse import broadcast_incident_update_to_user_connections
                    broadcast_incident_update_to_user_connections(
                        user_id,
                        {"type": "incident_update", "incident_id": str(incident_id), "source": SOURCE},
                        org_id=org_id,
                    )
                except Exception as e:
                    logger.warning("%s SSE notify failed: %s", log_prefix, e)

                from chat.background.summarization import generate_incident_summary
                generate_incident_summary.delay(
                    incident_id=str(incident_id),
                    user_id=user_id,
                    source_type=SOURCE,
                    alert_title=title,
                    severity=severity,
                    service=service,
                    raw_payload=rca_payload(evidence, tri, history),
                    alert_metadata=alert_metadata,
                )

                _trigger_rca(cursor, conn, user_id, incident_id, event_id, title, evidence, tri, history)
    except Exception as exc:
        logger.exception("%s failed for event %s", log_prefix, event_id)
        raise self.retry(exc=exc) from exc


def _trigger_rca(cursor, conn, user_id, incident_id, event_id, title, evidence, tri, history) -> None:
    from chat.background.task import (
        create_background_chat_session,
        is_background_chat_allowed,
        run_background_chat,
    )

    if not is_background_chat_allowed(user_id):
        logger.info("[PIPELINE][TASK] RCA rate-limited for user %s (event %s)", user_id, event_id)
        cursor.execute(
            "UPDATE incidents SET aurora_status = %s WHERE id = %s",
            ("rate_limited", str(incident_id)),
        )
        conn.commit()
        return

    trigger_metadata = {
        "source": SOURCE,
        "pipeline_event_id": event_id,
        "category": tri.get("category"),
        "pipeline": evidence.pipeline_name,
        "title": title,
    }
    session_id = create_background_chat_session(
        user_id=user_id,
        title=f"RCA: {title}",
        trigger_metadata=trigger_metadata,
        incident_id=str(incident_id),
    )
    prompt, _ = build_rca_prompt(SOURCE, title, rca_payload(evidence, tri, history), user_id=user_id)
    prompt += "\n" + render_prompt_section(evidence, tri, history)
    task = run_background_chat.delay(
        user_id=user_id,
        session_id=session_id,
        initial_message=prompt,
        trigger_metadata=trigger_metadata,
        incident_id=str(incident_id),
        send_notifications=False,
        rail_text=rail_text(evidence),
    )
    cursor.execute(
        "UPDATE incidents SET rca_celery_task_id = %s WHERE id = %s",
        (task.id, str(incident_id)),
    )
    conn.commit()
    logger.info("[PIPELINE][TASK] RCA queued for incident %s (task=%s)", incident_id, task.id)
