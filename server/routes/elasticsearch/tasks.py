"""Celery tasks for Elasticsearch integrations."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from celery_config import celery_app
from services.correlation.alert_correlator import AlertCorrelator
from services.correlation import handle_correlated_alert

logger = logging.getLogger(__name__)


def _should_trigger_background_chat(user_id: str, payload: Dict[str, Any]) -> bool:
    """Determine if a background chat should be triggered for this alert."""
    from utils.auth.stateless_auth import get_user_preference

    rca_enabled = get_user_preference(user_id, "elasticsearch_rca_enabled", default=False)
    if not rca_enabled:
        logger.debug(
            "[ELASTICSEARCH] Skipping background RCA - elasticsearch_rca_enabled preference disabled for user %s",
            user_id,
        )
        return False
    return True


def _build_rca_prompt_from_alert(
    payload: Dict[str, Any], user_id: Optional[str] = None
) -> str:
    """Build a simple user-visible prompt from an Elasticsearch alert payload.

    Args:
        payload: The Elasticsearch alert payload
        user_id: Optional user ID for Aurora Learn context injection
    """
    watch_id = payload.get("watch_id") or payload.get("alert_name") or payload.get("monitor_name") or "Unknown Alert"
    condition_met = payload.get("condition", {}).get("met", True)

    # Watcher nests under result.search, OpenSearch under ctx.payload
    search_result = payload.get("result", {}).get("search", {})
    query = (
        payload.get("input", {}).get("search", {}).get("request", {}).get("body", {}).get("query")
        or search_result.get("request", {}).get("body", {}).get("query")
        or payload.get("result", {}).get("input", {}).get("search", {}).get("request", {}).get("body", {}).get("query")
    )
    result_count = (
        payload.get("result_count")
        or search_result.get("total")
        or payload.get("ctx", {}).get("payload", {}).get("hits", {}).get("total", 0)
    )

    # Extract hits from Watcher (result.search.hits.hits) or OpenSearch (ctx.payload.hits.hits)
    results = (
        payload.get("results")
        or search_result.get("hits", {}).get("hits", [])
        or payload.get("ctx", {}).get("payload", {}).get("hits", {}).get("hits", [])
    )
    results_str = ""
    if results:
        if isinstance(results, list):
            results_str = "\n".join(f"  - {json.dumps(r)}" for r in results[:5])
        elif isinstance(results, dict):
            results_str = f"  - {json.dumps(results)}"

    prompt_parts = [
        "An Elasticsearch alert has been triggered and requires Root Cause Analysis.",
        "",
        "ALERT DETAILS:",
        f"- Watch/Alert Name: {watch_id}",
        f"- Condition Met: {condition_met}",
        f"- Result Count: {result_count}",
    ]

    if query:
        prompt_parts.append(f"- Query: {json.dumps(query)}")

    if results_str:
        prompt_parts.append("- Sample Results:")
        prompt_parts.append(results_str)

    # Add Aurora Learn context if available
    try:
        from chat.background.rca_prompt_builder import inject_aurora_learn_context

        service = payload.get("source") or payload.get("index") or ""
        inject_aurora_learn_context(
            prompt_parts, user_id, watch_id, service, "elasticsearch"
        )
    except Exception as e:
        logger.warning(f"[AURORA LEARN] Failed to get context: {e}")

    return "\n".join(prompt_parts)


def _extract_severity(payload: Dict[str, Any]) -> str:
    """Extract severity from Elasticsearch alert payload."""
    severity = (
        payload.get("severity")
        or payload.get("alert_severity")
        or payload.get("trigger", {}).get("severity")
        or payload.get("metadata", {}).get("severity")
        or payload.get("ctx", {}).get("metadata", {}).get("severity")
    )
    if severity:
        severity = str(severity).lower()
        if severity in ("critical", "high", "medium", "low"):
            return severity
    return "unknown"


def _extract_service(payload: Dict[str, Any]) -> str:
    """Extract service name from Elasticsearch payload."""
    service = (
        payload.get("source")
        or payload.get("index")
        or payload.get("watch_id")
        or payload.get("alert_name")
        or payload.get("monitor_name")
        or payload.get("metadata", {}).get("name")
        or "Elasticsearch Alert"
    )
    return str(service)[:255]


def _format_alert_summary(payload: Dict[str, Any]) -> str:
    """Format alert summary for logging."""
    name = payload.get("watch_id") or payload.get("alert_name") or payload.get("monitor_name") or "Unnamed Alert"
    result_count = (
        payload.get("result_count")
        or payload.get("result", {}).get("search", {}).get("total")
        or 0
    )
    return f"{name} (results={result_count})"


def _safe_json_dump(data: Dict[str, Any]) -> str:
    """Safe JSON serialization for logging."""
    try:
        return json.dumps(data, ensure_ascii=False)
    except Exception:
        return str(data)


@celery_app.task(
    bind=True, max_retries=3, default_retry_delay=30, name="elasticsearch.process_alert"
)
def process_elasticsearch_alert(
    self,
    payload: Dict[str, Any],
    metadata: Optional[Dict[str, Any]] = None,
    user_id: Optional[str] = None,
) -> None:
    """Background processor for Elasticsearch alert webhooks."""
    try:
        summary = _format_alert_summary(payload)
        logger.info("[ELASTICSEARCH][ALERT][USER:%s] %s", user_id or "unknown", summary)

        details = {
            "summary": summary,
            "payload": payload,
            "metadata": metadata or {},
            "user_id": user_id,
        }

        logger.debug("[ELASTICSEARCH][ALERT] full payload=%s", _safe_json_dump(details))

        if user_id:
            from utils.db.connection_pool import db_pool

            try:
                with db_pool.get_admin_connection() as conn:
                    with conn.cursor() as cursor:
                        received_at = datetime.now(timezone.utc)
                        alert_id = payload.get("watch_id") or payload.get("alert_id") or payload.get("metadata", {}).get("name")
                        alert_title = (
                            payload.get("alert_name")
                            or payload.get("watch_id")
                            or payload.get("monitor_name")
                            or payload.get("metadata", {}).get("name")
                            or "Elasticsearch Alert"
                        )
                        alert_state = "triggered"
                        watch_id = payload.get("watch_id") or payload.get("monitor_id") or payload.get("metadata", {}).get("name")
                        query = json.dumps(
                            payload.get("input", {}).get("search", {}).get("request", {}).get("body", {}).get("query")
                            or payload.get("result", {}).get("input", {}).get("search", {}).get("request", {}).get("body", {}).get("query")
                            or {}
                        )
                        result_count = (
                            payload.get("result_count")
                            or payload.get("result", {}).get("search", {}).get("total")
                        )
                        severity = _extract_severity(payload)

                        cursor.execute(
                            """
                            INSERT INTO elasticsearch_alerts
                            (user_id, alert_id, alert_title, alert_state, watch_id,
                             query, result_count, severity, payload, received_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            RETURNING id
                            """,
                            (
                                user_id,
                                alert_id,
                                alert_title,
                                alert_state,
                                watch_id,
                                query,
                                result_count,
                                severity,
                                json.dumps(payload),
                                received_at,
                            ),
                        )
                        alert_result = cursor.fetchone()
                        alert_db_id = alert_result[0] if alert_result else None

                        if not alert_db_id:
                            conn.rollback()
                            logger.error(
                                "[ELASTICSEARCH][ALERT] Failed to get alert_id for user %s",
                                user_id,
                            )
                            return

                        service = _extract_service(payload)

                        alert_metadata = {}
                        if query:
                            alert_metadata["query"] = query
                        if payload.get("index"):
                            alert_metadata["index"] = payload.get("index")
                        if payload.get("watch_id"):
                            alert_metadata["watchId"] = payload.get("watch_id")

                        correlation_title = alert_title or "Unknown Alert"

                        try:
                            correlator = AlertCorrelator()
                            correlation_result = correlator.correlate(
                                cursor=cursor,
                                user_id=user_id,
                                source_type="elasticsearch",
                                source_alert_id=alert_db_id,
                                alert_title=correlation_title,
                                alert_service=service,
                                alert_severity=severity,
                                alert_metadata=alert_metadata,
                            )

                            if correlation_result.is_correlated:
                                handle_correlated_alert(
                                    cursor=cursor,
                                    user_id=user_id,
                                    incident_id=correlation_result.incident_id,
                                    source_type="elasticsearch",
                                    source_alert_id=alert_db_id,
                                    alert_title=correlation_title,
                                    alert_service=service,
                                    alert_severity=severity,
                                    correlation_result=correlation_result,
                                    alert_metadata=alert_metadata,
                                    raw_payload=payload,
                                )
                                conn.commit()
                                return
                        except Exception as corr_exc:
                            logger.warning(
                                "[ELASTICSEARCH] Correlation check failed, proceeding with normal flow: %s",
                                corr_exc,
                            )

                        if not _should_trigger_background_chat(user_id, payload):
                            conn.commit()
                            logger.info(
                                "[ELASTICSEARCH][ALERT] Stored alert in database for user %s (RCA disabled, no incident created)",
                                user_id,
                            )
                            return

                        cursor.execute(
                            """
                            INSERT INTO incidents
                            (user_id, source_type, source_alert_id, alert_title, alert_service,
                             severity, status, started_at, alert_metadata)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (source_type, source_alert_id, user_id) WHERE org_id IS NULL DO UPDATE
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
                                "elasticsearch",
                                alert_db_id,
                                alert_title,
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
                        logger.info(
                            "[ELASTICSEARCH][ALERT] Stored alert and incident in database for user %s",
                            user_id,
                        )

                        try:
                            cursor.execute(
                                """INSERT INTO incident_alerts
                                   (user_id, incident_id, source_type, source_alert_id, alert_title, alert_service,
                                    alert_severity, correlation_strategy, correlation_score, alert_metadata)
                                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                                (
                                    user_id,
                                    incident_id,
                                    "elasticsearch",
                                    alert_db_id,
                                    alert_title,
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
                            logger.warning(
                                "[ELASTICSEARCH] Failed to record primary alert: %s", e
                            )

                    if incident_id:
                        logger.info(
                            "[ELASTICSEARCH][ALERT] Created incident %s for alert %s",
                            incident_id,
                            alert_db_id,
                        )

                        from chat.background.summarization import (
                            generate_incident_summary,
                        )

                        generate_incident_summary.delay(
                            incident_id=str(incident_id),
                            user_id=user_id,
                            source_type="elasticsearch",
                            alert_title=alert_title or "Unknown Alert",
                            severity=severity,
                            service=service,
                            raw_payload=payload,
                            alert_metadata=alert_metadata,
                        )
                        logger.info(
                            "[ELASTICSEARCH][ALERT] Triggered summary generation for incident %s",
                            incident_id,
                        )
                        try:
                            from chat.background.task import (
                                run_background_chat,
                                create_background_chat_session,
                                is_background_chat_allowed,
                            )

                            if not is_background_chat_allowed(user_id):
                                logger.info(
                                    "[ELASTICSEARCH][ALERT] Skipping background RCA - rate limited for user %s",
                                    user_id,
                                )
                            else:
                                chat_title = f"RCA: {alert_title or 'Elasticsearch Alert'}"
                                session_id = create_background_chat_session(
                                    user_id=user_id,
                                    title=chat_title,
                                    trigger_metadata={
                                        "source": "elasticsearch",
                                        "alert_id": alert_id,
                                        "watch_id": watch_id,
                                    },
                                )

                                rca_prompt = _build_rca_prompt_from_alert(
                                    payload, user_id=user_id
                                )

                                try:
                                    from chat.background.rca_prompt_builder import build_elasticsearch_rca_prompt
                                    rca_prompt = build_elasticsearch_rca_prompt(
                                        payload, user_id=user_id
                                    )
                                except Exception as prompt_exc:
                                    logger.warning(
                                        "[ELASTICSEARCH] Full prompt builder failed, using simple prompt: %s",
                                        prompt_exc,
                                    )

                                task = run_background_chat.delay(
                                    user_id=user_id,
                                    session_id=session_id,
                                    initial_message=rca_prompt,
                                    trigger_metadata={
                                        "source": "elasticsearch",
                                        "alert_id": alert_id,
                                        "alert_title": alert_title,
                                    },
                                    incident_id=str(incident_id)
                                    if incident_id
                                    else None,
                                )

                                if incident_id:
                                    cursor.execute(
                                        "UPDATE incidents SET rca_celery_task_id = %s WHERE id = %s",
                                        (task.id, str(incident_id))
                                    )
                                    conn.commit()

                                logger.info(
                                    "[ELASTICSEARCH][ALERT] Triggered background RCA chat for session %s (task_id=%s)",
                                    session_id,
                                    task.id,
                                )

                        except Exception as chat_exc:
                            logger.exception(
                                "[ELASTICSEARCH][ALERT] Failed to trigger background chat: %s",
                                chat_exc,
                            )

            except Exception as db_exc:
                logger.exception(
                    "[ELASTICSEARCH][ALERT] Failed to store alert in database: %s", db_exc
                )
        else:
            logger.warning(
                "[ELASTICSEARCH][ALERT] No user_id provided, alert not stored in database"
            )

    except Exception as exc:
        logger.exception("[ELASTICSEARCH][ALERT] Failed to process alert payload")
        raise self.retry(exc=exc)
