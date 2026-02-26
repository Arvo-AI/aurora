"""Celery tasks for Jenkins deployment event processing."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from celery_config import celery_app
from services.correlation.alert_correlator import AlertCorrelator
from services.correlation import handle_correlated_alert

logger = logging.getLogger(__name__)


def _extract_service(payload: Dict[str, Any]) -> str:
    """Extract a service name from the deployment event payload."""
    git_data = payload.get("git")
    repository = (
        git_data.get("repository", "").rstrip("/").rsplit("/", 1)[-1]
        if isinstance(git_data, dict)
        else ""
    )
    service = payload.get("service") or payload.get("job_name") or repository or "unknown"
    return str(service)[:255]


def _extract_severity(payload: Dict[str, Any]) -> str:
    """Map Jenkins build result to a severity level."""
    result = (payload.get("result") or "").upper()
    if result == "FAILURE":
        return "critical"
    if result == "UNSTABLE":
        return "high"
    if result == "ABORTED":
        return "medium"
    if result == "SUCCESS":
        return "low"
    return "unknown"


def _extract_git(payload: Dict[str, Any]) -> Dict[str, str]:
    """Normalise git fields from nested or flat payload."""
    git = payload.get("git", {})
    if isinstance(git, dict) and git:
        return {
            "commit_sha": git.get("commit_sha", ""),
            "branch": git.get("branch", ""),
            "repository": git.get("repository", ""),
        }
    return {
        "commit_sha": payload.get("commit_sha", ""),
        "branch": payload.get("branch", ""),
        "repository": payload.get("repository", ""),
    }


def _build_rca_prompt(payload: Dict[str, Any], user_id: Optional[str] = None) -> str:
    """Build an RCA prompt from a Jenkins deployment failure using the full prompt builder."""
    from chat.background.rca_prompt_builder import build_jenkins_rca_prompt
    return build_jenkins_rca_prompt(payload, user_id=user_id)


@celery_app.task(
    bind=True, max_retries=3, default_retry_delay=30, name="jenkins.process_deployment"
)
def process_jenkins_deployment(
    self,
    payload: Dict[str, Any],
    user_id: Optional[str] = None,
) -> None:
    """Process a Jenkins deployment event: persist, correlate, and optionally trigger RCA."""
    try:
        service = _extract_service(payload)
        result = (payload.get("result") or "UNKNOWN").upper()
        git = _extract_git(payload)
        environment = payload.get("environment", "")
        build_number = payload.get("build_number")
        build_url = payload.get("build_url", "")
        deployer = payload.get("deployer", "")
        duration_ms = payload.get("duration_ms")
        job_name = payload.get("job_name") or payload.get("service", "")
        trace_id = payload.get("trace_id", "") or ""
        span_id = payload.get("span_id", "") or ""

        logger.info(
            "[JENKINS][DEPLOY][USER:%s] %s → %s (env=%s, commit=%s)",
            user_id or "unknown", service, result, environment, (git.get("commit_sha") or "")[:8],
        )

        if not user_id:
            logger.warning("[JENKINS][DEPLOY] No user_id, event not stored")
            return

        from utils.db.connection_pool import db_pool

        received_at = datetime.now(timezone.utc)
        alert_id = None
        incident_id = None

        try:
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO jenkins_deployment_events
                           (user_id, event_type, service, environment, result, build_number,
                            build_url, commit_sha, branch, repository, deployer, duration_ms,
                            job_name, trace_id, span_id, payload, received_at)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                           RETURNING id""",
                        (
                            user_id,
                            payload.get("event_type", "deployment"),
                            service, environment, result, build_number, build_url,
                            git.get("commit_sha", ""), git.get("branch", ""),
                            git.get("repository", ""), deployer, duration_ms,
                            job_name,
                            trace_id if trace_id else None,
                            span_id if span_id else None,
                            json.dumps(payload), received_at,
                        ),
                    )
                    row = cursor.fetchone()
                    alert_id = row[0] if row else None
                    conn.commit()

                    if not alert_id:
                        logger.error("[JENKINS][DEPLOY] Failed to get event id for user %s", user_id)
                        return

                    logger.info("[JENKINS][DEPLOY] Stored event %s for user %s", alert_id, user_id)

                    severity = _extract_severity(payload)
                    alert_title = f"Jenkins deploy: {service} [{result}]"

                    alert_metadata = {
                        "buildUrl": build_url,
                        "buildNumber": build_number,
                        "environment": environment,
                        "result": result,
                        "deployer": deployer,
                    }
                    if git.get("commit_sha"):
                        alert_metadata["commitSha"] = git["commit_sha"]
                    if git.get("branch"):
                        alert_metadata["branch"] = git["branch"]
                    if git.get("repository"):
                        alert_metadata["repository"] = git["repository"]
                    if trace_id:
                        alert_metadata["traceId"] = trace_id

                    # --- Correlation: attach to existing open incident if temporal/service match ---
                    try:
                        correlator = AlertCorrelator()
                        correlation_result = correlator.correlate(
                            cursor=cursor,
                            user_id=user_id,
                            source_type="jenkins",
                            source_alert_id=alert_id,
                            alert_title=alert_title,
                            alert_service=service,
                            alert_severity=severity,
                            alert_metadata=alert_metadata,
                        )

                        if correlation_result.is_correlated:
                            handle_correlated_alert(
                                cursor=cursor,
                                user_id=user_id,
                                incident_id=correlation_result.incident_id,
                                source_type="jenkins",
                                source_alert_id=alert_id,
                                alert_title=alert_title,
                                alert_service=service,
                                alert_severity=severity,
                                correlation_result=correlation_result,
                                alert_metadata=alert_metadata,
                                raw_payload=payload,
                            )
                            conn.commit()
                            logger.info(
                                "[JENKINS][DEPLOY] Correlated with incident %s",
                                correlation_result.incident_id,
                            )

                            # Inject deployment context into running RCA
                            try:
                                from chat.background.context_updates import enqueue_rca_context_update

                                cursor.execute(
                                    "SELECT aurora_chat_session_id FROM incidents WHERE id = %s",
                                    (correlation_result.incident_id,),
                                )
                                inc_row = cursor.fetchone()
                                if inc_row and inc_row[0]:
                                    context_body = (
                                        f"Jenkins deployment detected for {service}:\n"
                                        f"- Result: {result}\n"
                                        f"- Environment: {environment}\n"
                                        f"- Commit: {git.get('commit_sha', 'unknown')}\n"
                                        f"- Build: {build_url}\n"
                                        f"- Deployer: {deployer}\n"
                                    )
                                    if trace_id:
                                        context_body += f"- OTel Trace ID: {trace_id}\n"
                                    enqueue_rca_context_update(
                                        user_id=user_id,
                                        session_id=str(inc_row[0]),
                                        source="jenkins",
                                        payload={"body": context_body, "title": alert_title, "service": service},
                                        incident_id=str(correlation_result.incident_id),
                                    )
                            except Exception as ctx_exc:
                                logger.warning("[JENKINS][DEPLOY] Failed to inject RCA context: %s", ctx_exc)

                            return
                    except Exception as corr_exc:
                        logger.warning("[JENKINS][DEPLOY] Correlation failed, continuing: %s", corr_exc)

                    # --- No correlation: create new incident only for failures ---
                    if result in ("FAILURE", "UNSTABLE"):
                        cursor.execute(
                            """INSERT INTO incidents
                               (user_id, source_type, source_alert_id, alert_title, alert_service,
                                alert_environment, severity, status, started_at, alert_metadata)
                               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                               ON CONFLICT (source_type, source_alert_id, user_id) DO UPDATE
                               SET updated_at = CURRENT_TIMESTAMP,
                                   alert_metadata = EXCLUDED.alert_metadata
                               RETURNING id""",
                            (
                                user_id, "jenkins", alert_id, alert_title, service,
                                environment, severity, "investigating", received_at,
                                json.dumps(alert_metadata),
                            ),
                        )
                        inc_row = cursor.fetchone()
                        incident_id = inc_row[0] if inc_row else None
                        conn.commit()

                        if incident_id:
                            # Record primary alert
                            try:
                                cursor.execute(
                                    """INSERT INTO incident_alerts
                                       (user_id, incident_id, source_type, source_alert_id, alert_title,
                                        alert_service, alert_severity, correlation_strategy, correlation_score,
                                        alert_metadata)
                                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                                    (
                                        user_id, incident_id, "jenkins", alert_id,
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
                                logger.warning("[JENKINS][DEPLOY] Failed to record primary alert: %s", e)

                    # --- SSE notification ---
                    if incident_id:
                        try:
                            from routes.incidents_sse import broadcast_incident_update_to_user_connections
                            broadcast_incident_update_to_user_connections(
                                user_id,
                                {"type": "incident_update", "incident_id": str(incident_id), "source": "jenkins"},
                            )
                        except Exception as e:
                            logger.warning("[JENKINS][DEPLOY] SSE notify failed: %s", e)

                        # Trigger summary generation
                        from chat.background.summarization import generate_incident_summary
                        generate_incident_summary.delay(
                            incident_id=str(incident_id),
                            user_id=user_id,
                            source_type="jenkins",
                            alert_title=alert_title,
                            severity=severity,
                            service=service,
                            raw_payload=payload,
                            alert_metadata=alert_metadata,
                        )

                        # Trigger RCA
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
                                        "source": "jenkins",
                                        "build_number": build_number,
                                        "result": result,
                                    },
                                )
                                rca_prompt = _build_rca_prompt(payload, user_id=user_id)
                                task = run_background_chat.delay(
                                    user_id=user_id,
                                    session_id=session_id,
                                    initial_message=rca_prompt,
                                    trigger_metadata={"source": "jenkins", "result": result},
                                    incident_id=str(incident_id),
                                )
                                cursor.execute(
                                    "UPDATE incidents SET rca_celery_task_id = %s WHERE id = %s",
                                    (task.id, str(incident_id)),
                                )
                                conn.commit()
                                logger.info(
                                    "[JENKINS][DEPLOY] Triggered RCA for incident %s (task=%s)",
                                    incident_id, task.id,
                                )
                            else:
                                logger.info("[JENKINS][DEPLOY] RCA rate-limited for user %s", user_id)
                        except Exception as rca_exc:
                            logger.exception("[JENKINS][DEPLOY] Failed to trigger RCA: %s", rca_exc)

        except Exception:
            logger.exception("[JENKINS][DEPLOY] DB error")
            raise

    except Exception as exc:
        logger.exception("[JENKINS][DEPLOY] Failed to process deployment event")
        raise self.retry(exc=exc) from exc
