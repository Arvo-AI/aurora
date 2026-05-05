"""Celery tasks for Spinnaker deployment event processing."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from celery_config import celery_app
from services.correlation.alert_correlator import AlertCorrelator
from services.correlation import handle_correlated_alert
from utils.auth.stateless_auth import set_rls_context

logger = logging.getLogger(__name__)


def _extract_service(payload: Dict[str, Any]) -> str:
    """Extract a service name from the Spinnaker deployment event payload."""
    service = payload.get("application") or payload.get("service") or "unknown"
    return str(service)[:255]


def _extract_severity(payload: Dict[str, Any]) -> str:
    """Map Spinnaker pipeline status to a severity level."""
    status = (payload.get("status") or "").upper()
    if status == "TERMINAL":
        return "critical"
    if status in ("CANCELED", "STOPPED"):
        return "medium"
    if status == "SUCCEEDED":
        return "low"
    if status == "RUNNING":
        return "low"
    return "unknown"


def _extract_execution_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise execution fields from nested or flat payload."""
    execution = payload.get("execution", {})
    if isinstance(execution, dict) and execution:
        return {
            "execution_id": execution.get("id", payload.get("execution_id", "")),
            "status": execution.get("status", payload.get("status", "")),
            "trigger_type": execution.get("trigger", {}).get("type", payload.get("trigger_type", "")),
            "trigger_user": execution.get("trigger", {}).get("user", payload.get("trigger_user", "")),
            "start_time": execution.get("startTime"),
            "end_time": execution.get("endTime"),
            "stages": execution.get("stages"),
            "parameters": execution.get("trigger", {}).get("parameters"),
        }
    return {
        "execution_id": payload.get("execution_id", ""),
        "status": payload.get("status", ""),
        "trigger_type": payload.get("trigger_type", ""),
        "trigger_user": payload.get("trigger_user", ""),
        "start_time": payload.get("start_time"),
        "end_time": payload.get("end_time"),
        "stages": payload.get("stages"),
        "parameters": payload.get("parameters"),
    }


def _build_rca_prompt(payload: Dict[str, Any], user_id: Optional[str] = None) -> tuple[str, str]:
    """Build an RCA prompt from a deployment failure."""
    from chat.background.rca_prompt_builder import build_spinnaker_rca_prompt
    return build_spinnaker_rca_prompt(payload, user_id=user_id)


@celery_app.task(
    bind=True, max_retries=3, default_retry_delay=30, name="spinnaker.process_deployment"
)
def process_spinnaker_deployment(
    self,
    payload: Dict[str, Any],
    user_id: Optional[str] = None,
) -> None:
    """Process a Spinnaker deployment event: persist, correlate, and optionally trigger RCA."""
    log_prefix = "[SPINNAKER][DEPLOY]"
    try:
        service = _extract_service(payload)
        exec_fields = _extract_execution_fields(payload)
        status = (exec_fields.get("status") or payload.get("status") or "UNKNOWN").upper()
        application = payload.get("application") or ""
        pipeline_name = payload.get("pipeline") or payload.get("pipeline_name") or ""
        execution_id = exec_fields.get("execution_id", "")
        execution_url = payload.get("execution_url", "")
        trigger_type = exec_fields.get("trigger_type", "")
        trigger_user = exec_fields.get("trigger_user", "")
        start_time = exec_fields.get("start_time")
        end_time = exec_fields.get("end_time")
        stages = exec_fields.get("stages")
        parameters = exec_fields.get("parameters")

        # Calculate duration (Spinnaker timestamps are epoch milliseconds)
        duration_ms = None
        if start_time and end_time:
            try:
                duration_ms = int(end_time) - int(start_time)  # millis - millis = millis
            except (ValueError, TypeError):
                logger.debug("%s Failed to compute duration from start=%s end=%s", log_prefix, start_time, end_time)

        logger.info(
            "%s[USER:%s] %s/%s → %s (trigger=%s)",
            log_prefix, user_id or "unknown", application, pipeline_name, status, trigger_type,
        )

        if not user_id:
            logger.warning("%s No user_id, event not stored", log_prefix)
            return

        from utils.db.connection_pool import db_pool

        received_at = datetime.now(timezone.utc)
        alert_id = None
        incident_id = None

        # Flatten payload for RCA prompt builder
        flat_payload = {
            **payload,
            "application": application,
            "pipeline_name": pipeline_name,
            "execution_id": execution_id,
            "status": status,
            "trigger_type": trigger_type,
            "trigger_user": trigger_user,
        }

        try:
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cursor:
                    org_id = set_rls_context(cursor, conn, user_id, log_prefix=log_prefix)
                    if not org_id:
                        logger.error("%s Cannot resolve org_id for user %s, aborting", log_prefix, user_id)
                        return

                    cursor.execute(
                        """INSERT INTO spinnaker_deployment_events
                           (user_id, org_id, event_type, application, pipeline_name, execution_id,
                            execution_url, status, trigger_type, trigger_user,
                            start_time, end_time, duration_ms, stages, parameters,
                            payload, received_at)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT (user_id, COALESCE(application, ''), COALESCE(execution_id, '')) DO UPDATE
                           SET status = EXCLUDED.status,
                               payload = EXCLUDED.payload,
                               received_at = EXCLUDED.received_at
                           RETURNING id""",
                        (
                            user_id, org_id,
                            payload.get("event_type", "pipeline"),
                            application, pipeline_name, execution_id,
                            execution_url, status, trigger_type, trigger_user,
                            datetime.fromtimestamp(start_time / 1000, tz=timezone.utc) if isinstance(start_time, (int, float)) and start_time else None,  # epoch ms → UTC
                            datetime.fromtimestamp(end_time / 1000, tz=timezone.utc) if isinstance(end_time, (int, float)) and end_time else None,  # epoch ms → UTC
                            duration_ms,
                            json.dumps(stages) if stages else None,
                            json.dumps(parameters) if parameters else None,
                            json.dumps(payload), received_at,
                        ),
                    )
                    row = cursor.fetchone()
                    alert_id = row[0] if row else None
                    conn.commit()

                    if not alert_id:
                        logger.error("%s Failed to get event id for user %s", log_prefix, user_id)
                        return

                    logger.info("%s Stored event %s for user %s", log_prefix, alert_id, user_id)

                    # Only create incidents for non-success results
                    if status in ("SUCCEEDED", "RUNNING", "NOT_STARTED"):
                        return

                    # Check if RCA is enabled for spinnaker
                    from utils.auth.stateless_auth import get_user_preference
                    rca_enabled = get_user_preference(user_id, "spinnaker_rca_enabled", default=True)
                    if not rca_enabled:
                        conn.commit()
                        logger.info(
                            "%s Stored deployment event for user %s (RCA disabled, no incident created)",
                            log_prefix, user_id,
                        )
                        return

                    severity = _extract_severity(payload)
                    alert_title = f"Spinnaker deploy: {application}/{pipeline_name} [{status}]"

                    alert_metadata = {
                        "application": application,
                        "pipelineName": pipeline_name,
                        "executionId": execution_id,
                        "executionUrl": execution_url,
                        "status": status,
                        "triggerType": trigger_type,
                        "triggerUser": trigger_user,
                    }

                    # --- Correlation ---
                    try:
                        cursor.execute("SAVEPOINT correlation_sp")
                        correlator = AlertCorrelator()
                        correlation_result = correlator.correlate(
                            cursor=cursor,
                            user_id=user_id,
                            source_type="spinnaker",
                            source_alert_id=alert_id,
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
                                source_type="spinnaker",
                                source_alert_id=alert_id,
                                alert_title=alert_title,
                                alert_service=service,
                                alert_severity=severity,
                                correlation_result=correlation_result,
                                alert_metadata=alert_metadata,
                                raw_payload=payload,
                                org_id=org_id,
                            )
                            conn.commit()
                            logger.info(
                                "%s Correlated with incident %s",
                                log_prefix, correlation_result.incident_id,
                            )
                            return

                        cursor.execute("RELEASE SAVEPOINT correlation_sp")
                    except Exception as corr_exc:
                        cursor.execute("ROLLBACK TO SAVEPOINT correlation_sp")
                        logger.warning("%s Correlation failed, continuing: %s", log_prefix, corr_exc)

                    # --- No correlation: create new incident ---
                    if status in ("TERMINAL", "CANCELED", "STOPPED"):
                        cursor.execute(
                            """INSERT INTO incidents
                               (user_id, org_id, source_type, source_alert_id, alert_title, alert_service,
                                severity, status, started_at, alert_metadata)
                               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                               ON CONFLICT (org_id, source_type, source_alert_id, user_id) DO UPDATE
                               SET updated_at = CURRENT_TIMESTAMP,
                                   alert_metadata = EXCLUDED.alert_metadata
                               RETURNING id""",
                            (
                                user_id, org_id, "spinnaker", alert_id, alert_title, service,
                                severity, "investigating", received_at,
                                json.dumps(alert_metadata),
                            ),
                        )
                        inc_row = cursor.fetchone()
                        incident_id = inc_row[0] if inc_row else None

                        if incident_id:
                            cursor.execute(
                                """INSERT INTO incident_alerts
                                   (user_id, org_id, incident_id, source_type, source_alert_id, alert_title,
                                    alert_service, alert_severity, correlation_strategy, correlation_score,
                                    alert_metadata)
                                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                                   ON CONFLICT DO NOTHING""",
                                (
                                    user_id, org_id, incident_id, "spinnaker", alert_id,
                                    alert_title, service, severity, "primary", 1.0,
                                    json.dumps(alert_metadata),
                                ),
                            )
                            cursor.execute(
                                "UPDATE incidents SET affected_services = ARRAY[%s] WHERE id = %s",
                                (service, incident_id),
                            )

                        conn.commit()

                    # --- Post-commit side effects ---
                    if incident_id:
                        try:
                            from routes.incidents_sse import broadcast_incident_update_to_user_connections
                            broadcast_incident_update_to_user_connections(
                                user_id,
                                {"type": "incident_update", "incident_id": str(incident_id), "source": "spinnaker"},
                                org_id=org_id,
                            )
                        except Exception as e:
                            logger.warning("%s SSE notify failed: %s", log_prefix, e)

                        from chat.background.summarization import generate_incident_summary
                        generate_incident_summary.delay(
                            incident_id=str(incident_id),
                            user_id=user_id,
                            source_type="spinnaker",
                            alert_title=alert_title,
                            severity=severity,
                            service=service,
                            raw_payload=payload,
                            alert_metadata=alert_metadata,
                        )

                        _trigger_rca(
                            cursor, conn, user_id, incident_id, alert_title,
                            status, flat_payload,
                        )

        except Exception:
            logger.exception("%s DB error", log_prefix)
            raise

    except Exception as exc:
        logger.exception("%s Failed to process deployment event", log_prefix)
        raise self.retry(exc=exc) from exc


def _trigger_rca(
    cursor, conn, user_id: str, incident_id, alert_title: str,
    status: str, payload: Dict[str, Any],
) -> None:
    """Trigger background RCA chat for a new incident."""
    log_prefix = "[SPINNAKER][DEPLOY]"
    try:
        from chat.background.task import (
            run_background_chat,
            create_background_chat_session,
            is_background_chat_allowed,
        )

        if is_background_chat_allowed(user_id):
            session_id = create_background_chat_session(
                user_id=user_id,
                title=f"RCA: {alert_title}",
                trigger_metadata={
                    "source": "spinnaker",
                    "status": status,
                },
                incident_id=str(incident_id),
            )
            rca_prompt, rail_text = _build_rca_prompt(payload, user_id=user_id)
            task = run_background_chat.delay(
                user_id=user_id,
                session_id=session_id,
                initial_message=rca_prompt,
                trigger_metadata={"source": "spinnaker", "status": status},
                incident_id=str(incident_id),
                rail_text=rail_text,
            )
            cursor.execute(
                "UPDATE incidents SET rca_celery_task_id = %s WHERE id = %s",
                (task.id, str(incident_id)),
            )
            conn.commit()
            logger.info(
                "%s Triggered RCA for incident %s (task=%s)",
                log_prefix, incident_id, task.id,
            )
        else:
            logger.info("%s RCA rate-limited for user %s", log_prefix, user_id)
    except Exception as rca_exc:
        logger.exception("%s Failed to trigger RCA: %s", log_prefix, rca_exc)
