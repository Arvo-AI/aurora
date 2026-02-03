"""Celery tasks for Netdata integrations."""

import json
import logging
import hashlib
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import psycopg2
from celery_config import celery_app
from utils.db.connection_pool import db_pool
from chat.background.task import (
    run_background_chat,
    create_background_chat_session,
    is_background_chat_allowed,
)
from routes.netdata.helpers import (
    format_alert_summary,
    generate_alert_hash,
    normalize_netdata_payload,
    should_trigger_background_chat,
    build_rca_prompt_from_alert,
)
from services.correlation.alert_correlator import AlertCorrelator

logger = logging.getLogger(__name__)

# Transient exceptions that warrant retry
TRANSIENT_EXCEPTIONS = (
    psycopg2.OperationalError,
    psycopg2.InterfaceError,
    ConnectionError,
    TimeoutError,
)


@celery_app.task(
    bind=True, max_retries=3, default_retry_delay=30, name="netdata.process_alert"
)
def process_netdata_alert(
    self,
    payload: Dict[str, Any],
    metadata: Optional[Dict[str, Any]] = None,
    user_id: Optional[str] = None,
) -> None:
    """Background processor for Netdata webhook alerts."""
    received_at = datetime.now(timezone.utc)

    # Normalize payload once using shared helper
    data = normalize_netdata_payload(payload)

    summary = format_alert_summary(data)
    logger.info("[NETDATA][ALERT][USER:%s] %s", user_id or "unknown", summary)

    if not user_id:
        logger.warning("[NETDATA][ALERT] No user_id provided, alert not stored")
        return

    # Generate hash for idempotent insert
    alert_hash = generate_alert_hash(user_id, data, received_at)

    try:
        with db_pool.get_admin_connection() as conn:
            try:
                with conn.cursor() as cursor:
                    # Use ON CONFLICT to make insert idempotent
                    cursor.execute(
                        """
                        INSERT INTO netdata_alerts 
                        (user_id, alert_name, alert_status, alert_class, alert_family, 
                         chart, host, space, room, value, message, payload, received_at, alert_hash)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (alert_hash) DO NOTHING
                        RETURNING id
                        """,
                        (
                            user_id,
                            data["name"],
                            data["status"],
                            data["class"],
                            data["family"],
                            data["chart"],
                            data["host"],
                            data["space"],
                            data["room"],
                            data["value"],
                            data["message"],
                            json.dumps(payload),
                            received_at,
                            alert_hash,
                        ),
                    )
                    alert_result = cursor.fetchone()
                    conn.commit()

                    if alert_result:
                        alert_id = alert_result[0]
                        logger.info(
                            "[NETDATA][ALERT] Stored alert in database for user %s (alert_id=%s)",
                            user_id,
                            alert_id,
                        )

                        # Create incident record
                        # Map Netdata status to severity levels
                        status = (data.get("status") or "").lower()
                        severity = {
                            "critical": "critical",
                            "warning": "high",
                            "clear": "low",
                        }.get(status, "unknown")
                        service = data["host"] or "unknown"

                        # Build alert metadata with Netdata-specific fields
                        alert_metadata = {}
                        if data["chart"]:
                            alert_metadata["chart"] = data["chart"]
                        if data["context"]:
                            alert_metadata["context"] = data["context"]
                        if data["space"]:
                            alert_metadata["space"] = data["space"]
                        if data["room"]:
                            alert_metadata["room"] = data["room"]
                        if data["duration"]:
                            alert_metadata["duration"] = data["duration"]
                        if data["alert_url"]:
                            alert_metadata["alertUrl"] = data["alert_url"]
                        if data["additional_critical"]:
                            alert_metadata["additionalCriticalAlerts"] = data[
                                "additional_critical"
                            ]
                        if data["additional_warning"]:
                            alert_metadata["additionalWarningAlerts"] = data[
                                "additional_warning"
                            ]
                        if data["value"]:
                            alert_metadata["value"] = data["value"]

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
                                            source_type="netdata",
                                            source_alert_id=alert_id,
                                            alert_title=data["name"],
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
                                        "netdata",
                                        alert_id,
                                        data["name"],
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
                                            "source": "netdata",
                                            "alert_title": data["name"],
                                            "correlation_score": correlation_result.score,
                                        },
                                    )
                                except Exception as e:
                                    logger.warning(
                                        "[NETDATA] Failed to notify SSE: %s", e
                                    )

                                logger.info(
                                    "[NETDATA] Alert correlated to incident %s (score=%.2f, strategy=%s)",
                                    incident_id,
                                    correlation_result.score,
                                    correlation_result.strategy,
                                )
                                return
                        except Exception as corr_exc:
                            logger.warning(
                                "[NETDATA] Correlation check failed, proceeding with normal flow: %s",
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
                                "netdata",
                                alert_id,
                                data["name"],
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
                                    "netdata",
                                    alert_id,
                                    data["name"],
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
                                "[NETDATA] Failed to record primary alert: %s", e
                            )

                        if incident_id:
                            logger.info(
                                "[NETDATA][ALERT] Created incident %s for alert %s",
                                incident_id,
                                alert_id,
                            )

                            # Trigger summary generation (always, fast)
                            from chat.background.summarization import (
                                generate_incident_summary,
                            )

                            generate_incident_summary.delay(
                                incident_id=str(incident_id),
                                user_id=user_id,
                                source_type="netdata",
                                alert_title=data["name"] or "Unknown Alert",
                                severity=severity,
                                service=service,
                                raw_payload=payload,
                                alert_metadata=alert_metadata,
                            )
                            logger.info(
                                "[NETDATA][ALERT] Triggered summary generation for incident %s",
                                incident_id,
                            )
                        else:
                            logger.error(
                                "[NETDATA][ALERT] Failed to create incident for alert %s (incident_row=%s)",
                                alert_id,
                                incident_row,
                            )

                        # Trigger background chat for RCA if enabled (only for new alerts)
                        if should_trigger_background_chat(user_id, payload):
                            try:
                                # Rate limit check - max 1 background chat per user per 5 minutes
                                if not is_background_chat_allowed(user_id):
                                    logger.info(
                                        "[NETDATA][ALERT] Skipping background RCA - rate limited for user %s",
                                        user_id,
                                    )
                                else:
                                    # Create a chat session for the background analysis
                                    chat_title = (
                                        f"RCA: {data['name'] or 'Netdata Alert'}"
                                    )
                                    session_id = create_background_chat_session(
                                        user_id=user_id,
                                        title=chat_title,
                                        trigger_metadata={
                                            "source": "netdata",
                                            "alert_name": data["name"],
                                            "alert_status": data["status"],
                                            "host": data["host"],
                                        },
                                    )

                                    # Build simple RCA prompt with Aurora Learn context injection
                                    rca_prompt = build_rca_prompt_from_alert(
                                        data, user_id=user_id
                                    )

                                    # Trigger the background chat task
                                    run_background_chat.delay(
                                        user_id=user_id,
                                        session_id=session_id,
                                        initial_message=rca_prompt,
                                        trigger_metadata={
                                            "source": "netdata",
                                            "alert_name": data["name"],
                                            "alert_status": data["status"],
                                            "host": data["host"],
                                            "chart": data["chart"],
                                        },
                                        incident_id=str(incident_id)
                                        if incident_id
                                        else None,
                                    )
                                    logger.info(
                                        "[NETDATA][ALERT] Triggered background RCA chat for session %s",
                                        session_id,
                                    )

                            except Exception as chat_exc:
                                logger.exception(
                                    "[NETDATA][ALERT] Failed to trigger background chat: %s",
                                    chat_exc,
                                )
                                # Don't raise - alert was still stored successfully
                    else:
                        logger.warning(
                            "[NETDATA][ALERT] Alert was not stored (likely duplicate alert_hash), skipping incident creation for user %s",
                            user_id,
                        )
                        return
            except Exception:
                conn.rollback()
                raise

    except TRANSIENT_EXCEPTIONS as exc:
        logger.warning("[NETDATA][ALERT] Transient error, will retry: %s", exc)
        raise self.retry(exc=exc)
    except Exception as exc:
        # Non-transient errors should not retry
        logger.exception(
            "[NETDATA][ALERT] Failed to process alert payload (non-retriable)"
        )
