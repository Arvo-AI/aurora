"""Celery tasks for Loki alert processing."""

import json
import logging
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
from routes.loki.helpers import (
    normalize_loki_webhook,
    generate_alert_hash,
    format_alert_summary,
    should_trigger_background_chat,
)
from chat.background.rca_prompt_builder import build_loki_rca_prompt
from services.correlation.alert_correlator import AlertCorrelator
from services.correlation import handle_correlated_alert

logger = logging.getLogger(__name__)

# Transient exceptions that warrant retry
TRANSIENT_EXCEPTIONS = (
    psycopg2.OperationalError,
    psycopg2.InterfaceError,
    ConnectionError,
    TimeoutError,
)


@celery_app.task(
    bind=True, max_retries=3, default_retry_delay=30, name="loki.process_alert"
)
def process_loki_alert(
    self,
    payload: Dict[str, Any],
    metadata: Optional[Dict[str, Any]] = None,
    user_id: Optional[str] = None,
) -> None:
    """Background processor for Loki webhook alerts.

    Handles multi-alert batches from Alertmanager v4 and Grafana Unified
    formats. Each alert in the payload's alerts[] array is normalized,
    deduplicated, stored, correlated, and triggers RCA independently.
    """
    received_at = datetime.now(timezone.utc)

    # Normalize payload -- returns a list (one entry per alert in the batch)
    normalized_alerts = normalize_loki_webhook(payload)

    if not normalized_alerts:
        logger.warning("[LOKI][ALERT] Empty or unparseable webhook payload")
        return

    if not user_id:
        logger.warning("[LOKI][ALERT] No user_id provided, alert not stored")
        return

    for normalized in normalized_alerts:
        summary = format_alert_summary(normalized)
        logger.info("[LOKI][ALERT][USER:%s] %s", user_id, summary)

        # Generate hash for idempotent insert
        alert_hash = generate_alert_hash(user_id, normalized, received_at)

        try:
            with db_pool.get_admin_connection() as conn:
                try:
                    with conn.cursor() as cursor:
                        from utils.auth.stateless_auth import set_rls_context

                        org_id = set_rls_context(
                            cursor, conn, user_id, log_prefix="[LOKI][ALERT]"
                        )
                        if not org_id:
                            return

                        # Use ON CONFLICT to make insert idempotent
                        cursor.execute(
                            """
                            INSERT INTO loki_alerts
                            (user_id, org_id, alert_uid, alert_title, alert_state,
                             rule_group, rule_name, labels, annotations, payload,
                             received_at, alert_hash)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (alert_hash) DO NOTHING
                            RETURNING id
                            """,
                            (
                                user_id,
                                org_id,
                                normalized["fingerprint"],
                                normalized["alert_name"],
                                normalized["alert_state"],
                                normalized["rule_group"],
                                normalized["rule_name"],
                                json.dumps(normalized["labels"]),
                                json.dumps(normalized["annotations"]),
                                json.dumps(payload),
                                received_at,
                                alert_hash,
                            ),
                        )
                        alert_result = cursor.fetchone()
                        conn.commit()

                        if not alert_result:
                            logger.warning(
                                "[LOKI][ALERT] Alert was not stored (likely duplicate alert_hash), "
                                "skipping incident creation for user %s",
                                user_id,
                            )
                            continue  # Next alert in the batch

                        alert_id = alert_result[0]
                        logger.info(
                            "[LOKI][ALERT] Stored alert in database for user %s (alert_id=%s)",
                            user_id,
                            alert_id,
                        )

                        # Severity and service already extracted during normalization
                        severity = normalized["severity"]
                        service = normalized["service"]

                        # Build alert metadata with Loki-specific fields
                        alert_metadata = {}
                        if normalized["labels"]:
                            alert_metadata["labels"] = normalized["labels"]
                        if normalized["annotations"]:
                            alert_metadata["annotations"] = normalized["annotations"]

                        summary_text = normalized["annotations"].get(
                            "summary"
                        ) or normalized["annotations"].get("description", "")
                        if summary_text:
                            alert_metadata["summary"] = summary_text

                        description_text = normalized["annotations"].get(
                            "description", ""
                        )
                        if description_text:
                            alert_metadata["description"] = description_text

                        if normalized["generator_url"]:
                            alert_metadata["generatorUrl"] = normalized["generator_url"]
                        if normalized["fingerprint"]:
                            alert_metadata["fingerprint"] = normalized["fingerprint"]
                        if normalized["rule_group"]:
                            alert_metadata["ruleGroup"] = normalized["rule_group"]
                        if normalized.get("dashboard_url"):
                            alert_metadata["dashboardUrl"] = normalized["dashboard_url"]
                        if normalized.get("silence_url"):
                            alert_metadata["silenceUrl"] = normalized["silence_url"]

                        # Attempt alert correlation
                        try:
                            correlator = AlertCorrelator()
                            correlation_result = correlator.correlate(
                                cursor=cursor,
                                user_id=user_id,
                                source_type="loki",
                                source_alert_id=alert_id,
                                alert_title=normalized["alert_name"],
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
                                    source_type="loki",
                                    source_alert_id=alert_id,
                                    alert_title=normalized["alert_name"],
                                    alert_service=service,
                                    alert_severity=severity,
                                    correlation_result=correlation_result,
                                    alert_metadata=alert_metadata,
                                    raw_payload=payload,
                                    org_id=org_id,
                                )
                                conn.commit()
                                continue  # Next alert in the batch
                        except Exception as corr_exc:
                            logger.warning(
                                "[LOKI] Correlation check failed, proceeding with normal flow: %s",
                                corr_exc,
                            )

                        # Create incident record
                        cursor.execute(
                            """
                            INSERT INTO incidents
                            (user_id, org_id, source_type, source_alert_id, alert_title,
                             alert_service, severity, status, started_at, alert_metadata)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (org_id, source_type, source_alert_id, user_id) DO UPDATE
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
                                org_id,
                                "loki",
                                alert_id,
                                normalized["alert_name"],
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

                        # Record primary alert in incident_alerts junction table
                        try:
                            cursor.execute(
                                """INSERT INTO incident_alerts
                                   (user_id, org_id, incident_id, source_type,
                                    source_alert_id, alert_title, alert_service,
                                    alert_severity, correlation_strategy,
                                    correlation_score, alert_metadata)
                                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                                (
                                    user_id,
                                    org_id,
                                    incident_id,
                                    "loki",
                                    alert_id,
                                    normalized["alert_name"],
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
                                "[LOKI] Failed to record primary alert: %s", e
                            )

                        if incident_id:
                            logger.info(
                                "[LOKI][ALERT] Created incident %s for alert %s",
                                incident_id,
                                alert_id,
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
                                        "source": "loki",
                                    },
                                )
                            except Exception as e:
                                logger.warning(
                                    "[LOKI][ALERT] Failed to notify SSE: %s", e
                                )

                            # Trigger summary generation (always, fast)
                            from chat.background.summarization import (
                                generate_incident_summary,
                            )

                            generate_incident_summary.delay(
                                incident_id=str(incident_id),
                                user_id=user_id,
                                source_type="loki",
                                alert_title=normalized["alert_name"] or "Unknown Alert",
                                severity=severity,
                                service=service,
                                raw_payload=payload,
                                alert_metadata=alert_metadata,
                            )
                            logger.info(
                                "[LOKI][ALERT] Triggered summary generation for incident %s",
                                incident_id,
                            )
                        else:
                            logger.error(
                                "[LOKI][ALERT] Failed to create incident for alert %s (incident_row=%s)",
                                alert_id,
                                incident_row,
                            )

                        # Trigger background chat for RCA if enabled (only for new alerts)
                        if should_trigger_background_chat(user_id, payload):
                            try:
                                if not is_background_chat_allowed(user_id):
                                    logger.info(
                                        "[LOKI][ALERT] Skipping background RCA - rate limited for user %s",
                                        user_id,
                                    )
                                else:
                                    session_id = create_background_chat_session(
                                        user_id=user_id,
                                        title=f"RCA: {normalized['alert_name'] or 'Loki Alert'}",
                                        trigger_metadata={
                                            "source": "loki",
                                            "alert_name": normalized["alert_name"],
                                            "alert_state": normalized["alert_state"],
                                            "service": service,
                                        },
                                        incident_id=str(incident_id)
                                        if incident_id
                                        else None,
                                    )

                                    rca_prompt = build_loki_rca_prompt(
                                        normalized, user_id=user_id
                                    )

                                    # Start RCA task and immediately store task ID
                                    task = run_background_chat.delay(
                                        user_id=user_id,
                                        session_id=session_id,
                                        initial_message=rca_prompt,
                                        trigger_metadata={
                                            "source": "loki",
                                            "alert_name": normalized["alert_name"],
                                            "alert_state": normalized["alert_state"],
                                            "service": service,
                                            "fingerprint": normalized["fingerprint"],
                                        },
                                        incident_id=str(incident_id)
                                        if incident_id
                                        else None,
                                    )

                                    # Store Celery task ID immediately for cancellation support
                                    if incident_id:
                                        cursor.execute(
                                            "UPDATE incidents SET rca_celery_task_id = %s WHERE id = %s",
                                            (task.id, str(incident_id)),
                                        )
                                        conn.commit()

                                    logger.info(
                                        "[LOKI][ALERT] Triggered background RCA for session %s (task_id=%s)",
                                        session_id,
                                        task.id,
                                    )

                            except Exception as chat_exc:
                                logger.exception(
                                    "[LOKI][ALERT] Failed to trigger background chat: %s",
                                    chat_exc,
                                )
                                # Don't raise - alert was still stored successfully

                except Exception:
                    conn.rollback()
                    raise

        except TRANSIENT_EXCEPTIONS as exc:
            logger.warning("[LOKI][ALERT] Transient error, will retry: %s", exc)
            raise self.retry(exc=exc)
        except Exception as exc:
            # Non-transient errors should not retry
            logger.exception(
                "[LOKI][ALERT] Failed to process alert payload (non-retriable)"
            )
