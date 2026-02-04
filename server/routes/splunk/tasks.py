"""Celery tasks for Splunk integrations."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from celery_config import celery_app
from services.correlation.alert_correlator import AlertCorrelator

logger = logging.getLogger(__name__)


def _should_trigger_background_chat(user_id: str, payload: Dict[str, Any]) -> bool:
    """Determine if a background chat should be triggered for this alert."""
    from utils.auth.stateless_auth import get_user_preference

    rca_enabled = get_user_preference(user_id, "splunk_rca_enabled", default=False)
    if not rca_enabled:
        logger.debug(
            "[SPLUNK] Skipping background RCA - splunk_rca_enabled preference disabled for user %s",
            user_id,
        )
        return False
    return True


def _build_rca_prompt_from_alert(
    payload: Dict[str, Any], user_id: Optional[str] = None
) -> str:
    """Build a simple user-visible prompt from a Splunk alert payload.

    Note: Detailed RCA instructions are injected via system prompt (rca_context),
    not in this user message.

    Args:
        payload: The Splunk alert payload
        user_id: Optional user ID for Aurora Learn context injection
    """
    search_name = payload.get("search_name") or payload.get("name") or "Unknown Alert"
    result_count = payload.get("result_count") or payload.get("results_count") or 0
    search_query = payload.get("search") or payload.get("search_query") or ""

    # Extract result sample if available
    results = payload.get("results") or payload.get("result") or []
    results_str = ""
    if results:
        if isinstance(results, list):
            results_str = "\n".join(f"  - {json.dumps(r)}" for r in results[:5])
        elif isinstance(results, dict):
            results_str = f"  - {json.dumps(results)}"

    prompt_parts = [
        "A Splunk alert has been triggered and requires Root Cause Analysis.",
        "",
        "ALERT DETAILS:",
        f"- Search Name: {search_name}",
        f"- Result Count: {result_count}",
    ]

    if search_query:
        prompt_parts.append(f"- SPL Query: {search_query}")

    if results_str:
        prompt_parts.append("- Sample Results:")
        prompt_parts.append(results_str)

    # Add Aurora Learn context if available
    try:
        from chat.background.rca_prompt_builder import inject_aurora_learn_context

        service = payload.get("app") or payload.get("source") or ""
        inject_aurora_learn_context(
            prompt_parts, user_id, search_name, service, "splunk"
        )
    except Exception as e:
        logger.warning(f"[AURORA LEARN] Failed to get context: {e}")

    return "\n".join(prompt_parts)


def _extract_severity(payload: Dict[str, Any]) -> str:
    """Extract severity from Splunk alert payload."""
    # Check for severity in payload
    severity = payload.get("severity") or payload.get("alert_severity")
    if severity:
        severity = str(severity).lower()
        if severity in ("critical", "high", "medium", "low"):
            return severity
        # Splunk uses numeric severity (1-6)
        try:
            sev_num = int(severity)
            if sev_num <= 2:
                return "critical"
            elif sev_num <= 3:
                return "high"
            elif sev_num <= 4:
                return "medium"
            else:
                return "low"
        except ValueError:
            pass
    return "unknown"


def _extract_service(payload: Dict[str, Any]) -> str:
    """Extract service name from Splunk payload."""
    service = (
        payload.get("app")
        or payload.get("source")
        or payload.get("sourcetype")
        or payload.get("search_name")
        or "unknown"
    )
    return str(service)[:255]


def _format_alert_summary(payload: Dict[str, Any]) -> str:
    """Format alert summary for logging."""
    name = payload.get("search_name") or payload.get("name") or "Unnamed Alert"
    result_count = payload.get("result_count") or payload.get("results_count") or 0
    return f"{name} (results={result_count})"


def _safe_json_dump(data: Dict[str, Any]) -> str:
    """Safe JSON serialization for logging."""
    try:
        return json.dumps(data, ensure_ascii=False)
    except Exception:
        return str(data)


@celery_app.task(
    bind=True, max_retries=3, default_retry_delay=30, name="splunk.process_alert"
)
def process_splunk_alert(
    self,
    payload: Dict[str, Any],
    metadata: Optional[Dict[str, Any]] = None,
    user_id: Optional[str] = None,
) -> None:
    """Background processor for Splunk alert webhooks."""
    try:
        received_at = datetime.now(timezone.utc)
        summary = _format_alert_summary(payload)
        logger.info("[SPLUNK][ALERT][USER:%s] %s", user_id or "unknown", summary)

        details = {
            "received_at": received_at.isoformat(),
            "summary": summary,
            "payload": payload,
            "metadata": metadata or {},
            "user_id": user_id,
        }

        logger.debug("[SPLUNK][ALERT] full payload=%s", _safe_json_dump(details))

        if user_id:
            from utils.db.connection_pool import db_pool

            try:
                with db_pool.get_admin_connection() as conn:
                    with conn.cursor() as cursor:
                        # Extract fields from Splunk webhook payload
                        alert_id = payload.get("sid") or payload.get("search_id")
                        alert_title = payload.get("search_name") or payload.get("name")
                        alert_state = "triggered"
                        search_name = payload.get("search_name") or payload.get("name")
                        search_query = payload.get("search") or payload.get(
                            "search_query"
                        )
                        result_count = payload.get("result_count") or payload.get(
                            "results_count"
                        )
                        severity = _extract_severity(payload)

                        cursor.execute(
                            """
                            INSERT INTO splunk_alerts
                            (user_id, alert_id, alert_title, alert_state, search_name,
                             search_query, result_count, severity, payload, received_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            RETURNING id
                            """,
                            (
                                user_id,
                                alert_id,
                                alert_title,
                                alert_state,
                                search_name,
                                search_query,
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
                                "[SPLUNK][ALERT] Failed to get alert_id for user %s",
                                user_id,
                            )
                            return

                        conn.commit()
                        logger.debug(
                            "[SPLUNK][ALERT] Alert record created for user %s",
                            user_id,
                        )

                        service = _extract_service(payload)

                        # Build alert metadata
                        alert_metadata = {}
                        if search_query:
                            alert_metadata["searchQuery"] = search_query
                        if payload.get("results_link"):
                            alert_metadata["resultsLink"] = payload.get("results_link")
                        if payload.get("app"):
                            alert_metadata["app"] = payload.get("app")
                        if payload.get("owner"):
                            alert_metadata["owner"] = payload.get("owner")

                        correlation_title = alert_title or "Unknown Alert"

                        try:
                            correlator = AlertCorrelator()
                            correlation_result = None
                            with db_pool.get_admin_connection() as correlation_conn:
                                previous_autocommit = correlation_conn.autocommit
                                correlation_conn.autocommit = True
                                try:
                                    with (
                                        correlation_conn.cursor() as correlation_cursor
                                    ):
                                        correlation_result = correlator.correlate(
                                            cursor=correlation_cursor,
                                            user_id=user_id,
                                            source_type="splunk",
                                            source_alert_id=alert_db_id,
                                            alert_title=correlation_title,
                                            alert_service=service,
                                            alert_severity=severity,
                                            alert_metadata=alert_metadata,
                                        )
                                finally:
                                    correlation_conn.autocommit = previous_autocommit

                            if correlation_result and correlation_result.is_correlated:
                                incident_id = correlation_result.incident_id
                                cursor.execute(
                                    """INSERT INTO incident_alerts
                                       (incident_id, source_type, source_alert_id, alert_title, alert_service,
                                        alert_severity, correlation_strategy, correlation_score,
                                        correlation_details, alert_metadata)
                                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                                    (
                                        incident_id,
                                        "splunk",
                                        alert_db_id,
                                        correlation_title,
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
                                            "source": "splunk",
                                            "alert_title": alert_title,
                                            "correlation_score": correlation_result.score,
                                        },
                                    )
                                except Exception as e:
                                    logger.warning(
                                        "[SPLUNK] Failed to notify SSE: %s", e
                                    )

                                logger.info(
                                    "[SPLUNK] Alert correlated to incident %s (score=%.2f, strategy=%s)",
                                    incident_id,
                                    correlation_result.score,
                                    correlation_result.strategy,
                                )
                                return
                        except Exception as corr_exc:
                            logger.warning(
                                "[SPLUNK] Correlation check failed, proceeding with normal flow: %s",
                                corr_exc,
                            )

                        # Check if RCA is enabled before creating incident
                        if not _should_trigger_background_chat(user_id, payload):
                            # RCA disabled - just commit the alert and return
                            conn.commit()
                            logger.info(
                                "[SPLUNK][ALERT] Stored alert in database for user %s (RCA disabled, no incident created)",
                                user_id,
                            )
                            return

                        # RCA enabled - create incident record

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
                                "splunk",
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

                        # Commit both alert and incident atomically
                        conn.commit()
                        logger.info(
                            "[SPLUNK][ALERT] Stored alert and incident in database for user %s",
                            user_id,
                        )

                        try:
                            cursor.execute(
                                """INSERT INTO incident_alerts
                                   (incident_id, source_type, source_alert_id, alert_title, alert_service,
                                    alert_severity, correlation_strategy, correlation_score, alert_metadata)
                                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                                (
                                    incident_id,
                                    "splunk",
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
                                "UPDATE incidents SET affected_services = ARRAY[%s] WHERE id = %s AND (affected_services IS NULL OR affected_services = '{}')",
                                (service, incident_id),
                            )
                            conn.commit()
                        except Exception as e:
                            logger.warning(
                                "[SPLUNK] Failed to record primary alert: %s", e
                            )

                    if incident_id:
                        logger.info(
                            "[SPLUNK][ALERT] Created incident %s for alert %s",
                            incident_id,
                            alert_db_id,
                        )

                        # Trigger summary generation
                        from chat.background.summarization import (
                            generate_incident_summary,
                        )

                        generate_incident_summary.delay(
                            incident_id=str(incident_id),
                            user_id=user_id,
                            source_type="splunk",
                            alert_title=alert_title or "Unknown Alert",
                            severity=severity,
                            service=service,
                            raw_payload=payload,
                            alert_metadata=alert_metadata,
                        )
                        logger.info(
                            "[SPLUNK][ALERT] Triggered summary generation for incident %s",
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
                                    "[SPLUNK][ALERT] Skipping background RCA - rate limited for user %s",
                                    user_id,
                                )
                            else:
                                chat_title = f"RCA: {alert_title or 'Splunk Alert'}"
                                session_id = create_background_chat_session(
                                    user_id=user_id,
                                    title=chat_title,
                                    trigger_metadata={
                                        "source": "splunk",
                                        "alert_id": alert_id,
                                        "search_name": search_name,
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
                                        "source": "splunk",
                                        "alert_id": alert_id,
                                        "alert_title": alert_title,
                                    },
                                    incident_id=str(incident_id)
                                    if incident_id
                                    else None,
                                )
                                logger.info(
                                    "[SPLUNK][ALERT] Triggered background RCA chat for session %s",
                                    session_id,
                                )

                        except Exception as chat_exc:
                            logger.exception(
                                "[SPLUNK][ALERT] Failed to trigger background chat: %s",
                                chat_exc,
                            )

            except Exception as db_exc:
                logger.exception(
                    "[SPLUNK][ALERT] Failed to store alert in database: %s", db_exc
                )
        else:
            logger.warning(
                "[SPLUNK][ALERT] No user_id provided, alert not stored in database"
            )

    except Exception as exc:
        logger.exception("[SPLUNK][ALERT] Failed to process alert payload")
        raise self.retry(exc=exc)
