"""Celery tasks for CloudWatch alarm webhook processing.

AWS SNS delivers CloudWatch alarm state-change notifications as HTTP POST requests.
Each notification contains a single alarm state transition. We process each notification
to:
- Persist the raw alarm to cloudwatch_alarms for audit / MCP queries.
- Create an incident when the alarm enters ALARM state.
- Correlate ALARM->OK transitions back to the original incident (auto-resolve).
- Trigger a background RCA chat for ALARM-state notifications.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from celery_config import celery_app
from services.correlation.alert_correlator import AlertCorrelator
from services.correlation import handle_correlated_alert

logger = logging.getLogger(__name__)

# CloudWatch alarm states
_ALARM_STATE = "ALARM"
_OK_STATE = "OK"


def _is_alarm_firing(state_value: str) -> bool:
    return (state_value or "").upper() == _ALARM_STATE


def _is_alarm_resolved(state_value: str) -> bool:
    return (state_value or "").upper() == _OK_STATE


def _extract_severity(state_value: str, payload: Dict[str, Any]) -> str:
    """Map CloudWatch alarm state to Aurora severity."""
    state = (state_value or "").upper()
    if state == _ALARM_STATE:
        alarm_name = (payload.get("AlarmName") or "").lower()
        if "critical" in alarm_name:
            return "critical"
        return "high"
    return "unknown"


def _extract_service(payload: Dict[str, Any]) -> str:
    """Best-effort service extraction from a CloudWatch alarm payload."""
    # Dimensions carry service/resource context (e.g. {"InstanceId": "i-123"})
    trigger = payload.get("Trigger") or {}
    dimensions = trigger.get("Dimensions") or []
    if dimensions and isinstance(dimensions, list):
        dim = dimensions[0]
        val = dim.get("value") or dim.get("Value")
        name = dim.get("name") or dim.get("Name")
        if val:
            return str(val)[:255]
        if name:
            return str(name)[:255]
    namespace = trigger.get("Namespace") or payload.get("Namespace") or ""
    return str(namespace)[:255] or "aws"


def _safe_json(data: Any) -> str:
    try:
        return json.dumps(data, ensure_ascii=False)
    except Exception:
        return str(data)


class AlarmFields:
    """Bundle of fields extracted from a CloudWatch alarm payload."""
    __slots__ = (
        'alarm_name', 'alarm_arn', 'state_value', 'previous_state',
        'reason', 'account_id', 'region',
    )

    def __init__(
        self, alarm_name: str, alarm_arn: str, state_value: str,
        previous_state: str, reason: str, account_id: str, region: str,
    ):
        self.alarm_name = alarm_name
        self.alarm_arn = alarm_arn
        self.state_value = state_value
        self.previous_state = previous_state
        self.reason = reason
        self.account_id = account_id
        self.region = region


def _persist_alarm(
    cursor, conn, user_id: str, org_id: str, fields: AlarmFields,
    payload: Dict[str, Any], received_at, sns_message_id: str = "",
):
    """Insert raw alarm into cloudwatch_alarms. Returns (alarm_db_id, was_inserted) or (None, False).

    Uses sns_message_id for deduplication — if the same SNS message is delivered
    more than once (retry), we return the existing row instead of creating a duplicate.
    """
    if sns_message_id:
        cursor.execute(
            "SELECT id FROM cloudwatch_alarms WHERE sns_message_id = %s AND user_id = %s",
            (sns_message_id, user_id),
        )
        existing = cursor.fetchone()
        if existing:
            logger.info(
                "[CLOUDWATCH][ALARM] Dedup: sns_message_id=%s already stored (db_id=%s)",
                sns_message_id, existing[0],
            )
            return existing[0], False

    cursor.execute(
        """
        INSERT INTO cloudwatch_alarms
          (user_id, org_id, alarm_name, alarm_arn, state_value,
           previous_state_value, reason, account_id, region, payload, received_at,
           sns_message_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            user_id, org_id, fields.alarm_name, fields.alarm_arn, fields.state_value,
            fields.previous_state, fields.reason, fields.account_id, fields.region,
            json.dumps(payload), received_at, sns_message_id or None,
        ),
    )
    row = cursor.fetchone()
    alarm_db_id = row[0] if row else None
    conn.commit()

    if not alarm_db_id:
        logger.error("[CLOUDWATCH][ALARM] Failed to persist alarm for user %s", user_id)
        return None, False

    logger.info(
        "[CLOUDWATCH][ALARM] Stored alarm (db_id=%s) for user %s",
        alarm_db_id, user_id,
    )
    return alarm_db_id, True


def _handle_resolved_alarm(
    cursor, conn, user_id: str, org_id: str, alarm_name: str,
    alarm_db_id, state_value: str, payload: Dict[str, Any],
) -> None:
    """Correlate a resolved alarm back to its original incident and mark it resolved."""
    account_id = payload.get("AWSAccountId") or payload.get("account_id") or ""
    region = payload.get("Region") or payload.get("region") or ""
    cursor.execute(
        """
        SELECT id FROM incidents
        WHERE user_id = %s AND source_type = 'cloudwatch'
          AND alert_metadata::jsonb ->> 'alarm_name' = %s
          AND (%s = '' OR alert_metadata::jsonb ->> 'account_id' = %s)
          AND (%s = '' OR alert_metadata::jsonb ->> 'region' = %s)
          AND status NOT IN ('resolved', 'closed')
        ORDER BY started_at DESC LIMIT 1
        """,
        (user_id, alarm_name, account_id, account_id, region, region),
    )
    row = cursor.fetchone()
    if row:
        original_incident_id = row[0]
        cursor.execute(
            """
            INSERT INTO incident_alerts
              (user_id, org_id, incident_id, source_type, source_alert_id,
               alert_title, alert_service, alert_severity, correlation_strategy,
               correlation_score, alert_metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                user_id, org_id, original_incident_id, "cloudwatch",
                alarm_db_id, alarm_name,
                _extract_service(payload),
                _extract_severity(state_value, payload),
                "resolved_webhook", 1.0,
                json.dumps({"resolved_webhook": True, "alarm_name": alarm_name}),
            ),
        )
        cursor.execute(
            """
            UPDATE incidents SET status = 'resolved', resolved_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (original_incident_id,),
        )
        cursor.execute(
            """
            INSERT INTO incident_lifecycle_events
              (incident_id, user_id, org_id, event_type, new_value)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (original_incident_id, user_id, org_id, "status_change", "resolved"),
        )
        conn.commit()
        logger.info(
            "[CLOUDWATCH][ALARM] Resolved alarm (%s) — incident %s marked resolved",
            alarm_name, original_incident_id,
        )
    else:
        logger.info(
            "[CLOUDWATCH][ALARM] Resolved alarm for user %s, no matching incident. Skipping.",
            user_id,
        )


def _build_alert_metadata(
    alarm_name: str, alarm_arn: str, account_id: str,
    region: str, reason: str, previous_state: str, payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Construct the alert_metadata dict for a CloudWatch alarm."""
    alert_metadata: Dict[str, Any] = {
        "alarm_name": alarm_name,
        "alarm_arn": alarm_arn,
        "account_id": account_id,
        "region": region,
        "reason": reason,
        "previous_state": previous_state,
    }
    trigger = payload.get("Trigger") or {}
    if trigger.get("Namespace"):
        alert_metadata["namespace"] = trigger["Namespace"]
    if trigger.get("MetricName"):
        alert_metadata["metric_name"] = trigger["MetricName"]
    if trigger.get("Dimensions"):
        alert_metadata["dimensions"] = trigger["Dimensions"]
    return alert_metadata


def _try_correlate_alarm(
    cursor, conn, user_id: str, org_id: str, alarm_db_id,
    alarm_name: str, service: str, severity: str,
    alert_metadata: Dict[str, Any], payload: Dict[str, Any],
) -> bool:
    """Attempt to correlate the alarm with an existing incident. Returns True if correlated."""
    correlation_result = None
    try:
        cursor.execute("SAVEPOINT sp_correlation")
        correlator = AlertCorrelator()
        correlation_result = correlator.correlate(
            cursor=cursor, user_id=user_id, source_type="cloudwatch",
            source_alert_id=alarm_db_id, alert_title=alarm_name,
            alert_service=service, alert_severity=severity,
            alert_metadata=alert_metadata, org_id=org_id,
        )
        cursor.execute("RELEASE SAVEPOINT sp_correlation")
    except Exception as exc:
        cursor.execute("ROLLBACK TO SAVEPOINT sp_correlation")
        logger.warning("[CLOUDWATCH][ALARM] Correlation check failed: %s", exc)

    if not (correlation_result and correlation_result.is_correlated):
        return False

    try:
        cursor.execute("SAVEPOINT sp_handle_correlated")
        handle_correlated_alert(
            cursor=cursor, user_id=user_id,
            incident_id=correlation_result.incident_id,
            source_type="cloudwatch", source_alert_id=alarm_db_id,
            alert_title=alarm_name, alert_service=service,
            alert_severity=severity,
            correlation_result=correlation_result,
            alert_metadata=alert_metadata, raw_payload=payload,
            org_id=org_id,
        )
        cursor.execute("RELEASE SAVEPOINT sp_handle_correlated")
        conn.commit()
        logger.info(
            "[CLOUDWATCH][ALARM] Correlated alarm '%s' to incident %s",
            alarm_name, correlation_result.incident_id,
        )
    except Exception as exc:
        cursor.execute("ROLLBACK TO SAVEPOINT sp_handle_correlated")
        logger.warning("[CLOUDWATCH][ALARM] handle_correlated_alert failed: %s", exc)
        return False
    return True


def _create_incident_record(
    cursor, conn, user_id: str, org_id: str, alarm_db_id,
    alarm_name: str, service: str, severity: str,
    alert_metadata: Dict[str, Any], received_at,
):
    """Insert the incident + lifecycle event + alert link. Returns incident_id or None."""
    cursor.execute(
        """
        INSERT INTO incidents
          (user_id, org_id, source_type, source_alert_id, alert_title, alert_service,
           severity, status, started_at, alert_metadata, alert_fired_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (org_id, source_type, source_alert_id, user_id) DO UPDATE
          SET updated_at = CURRENT_TIMESTAMP,
              started_at = CASE WHEN incidents.status != 'analyzed'
                           THEN EXCLUDED.started_at ELSE incidents.started_at END,
              alert_metadata = EXCLUDED.alert_metadata
        RETURNING id, (xmax = 0) AS inserted
        """,
        (
            user_id, org_id, "cloudwatch", alarm_db_id, alarm_name,
            service, severity, "investigating", received_at,
            json.dumps(alert_metadata), received_at,
        ),
    )
    incident_row = cursor.fetchone()
    incident_id = incident_row[0] if incident_row else None
    incident_was_inserted = bool(incident_row[1]) if incident_row else False
    conn.commit()

    if not incident_id:
        logger.error("[CLOUDWATCH][ALARM] Failed to create incident for alarm %s", alarm_name)
        return None

    if incident_was_inserted:
        try:
            cursor.execute("SAVEPOINT sp_lifecycle")
            cursor.execute(
                """
                INSERT INTO incident_lifecycle_events
                  (incident_id, user_id, org_id, event_type, new_value)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (incident_id, user_id, org_id, "created", "investigating"),
            )
            cursor.execute("RELEASE SAVEPOINT sp_lifecycle")
            conn.commit()
        except Exception as exc:
            cursor.execute("ROLLBACK TO SAVEPOINT sp_lifecycle")
            logger.warning("[CLOUDWATCH][ALARM] Failed to record lifecycle event: %s", exc)

    try:
        cursor.execute("SAVEPOINT sp_incident_alerts")
        cursor.execute(
            """
            INSERT INTO incident_alerts
              (user_id, org_id, incident_id, source_type, source_alert_id, alert_title,
               alert_service, alert_severity, correlation_strategy, correlation_score,
               alert_metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                user_id, org_id, incident_id, "cloudwatch", alarm_db_id, alarm_name,
                service, severity, "primary", 1.0, json.dumps(alert_metadata),
            ),
        )
        cursor.execute(
            "UPDATE incidents SET affected_services = ARRAY[%s] WHERE id = %s",
            (service, incident_id),
        )
        cursor.execute("RELEASE SAVEPOINT sp_incident_alerts")
        conn.commit()
    except Exception as exc:
        cursor.execute("ROLLBACK TO SAVEPOINT sp_incident_alerts")
        logger.warning("[CLOUDWATCH][ALARM] Failed to record primary alert: %s", exc)

    return incident_id


def _post_incident_actions(
    user_id: str, org_id: str, incident_id, alarm_name: str,
    service: str, severity: str, state_value: str,
    alert_metadata: Dict[str, Any], payload: Dict[str, Any],
    skip_rca: bool, cursor, conn,
) -> None:
    """SSE broadcast, summary generation, and RCA trigger."""
    try:
        from routes.incidents_sse import broadcast_incident_update_to_user_connections
        broadcast_incident_update_to_user_connections(
            user_id,
            {"type": "incident_update", "incident_id": str(incident_id), "source": "cloudwatch"},
            org_id=org_id,
        )
    except Exception as exc:
        logger.warning("[CLOUDWATCH][ALARM] Failed to notify SSE: %s", exc)

    try:
        from chat.background.summarization import generate_incident_summary
        generate_incident_summary.delay(
            incident_id=str(incident_id), user_id=user_id, source_type="cloudwatch",
            alert_title=alarm_name, severity=severity,
            service=service, raw_payload=payload, alert_metadata=alert_metadata,
        )
    except Exception as exc:
        logger.warning(
            "[CLOUDWATCH][ALARM] Failed to enqueue summary for incident %s: %s",
            incident_id, exc,
        )

    if skip_rca:
        return

    try:
        from chat.background.task import (
            run_background_chat,
            create_background_chat_session,
            is_background_chat_allowed,
        )
        if not is_background_chat_allowed(user_id):
            logger.info("[CLOUDWATCH][ALARM] Skipping RCA — rate limited for user %s", user_id)
            return

        from chat.background.rca_prompt_builder import build_cloudwatch_rca_prompt
        rca_prompt, rail_text = build_cloudwatch_rca_prompt(payload, user_id=user_id)
        chat_title = f"RCA: {alarm_name}"
        session_id = create_background_chat_session(
            user_id=user_id,
            title=chat_title,
            trigger_metadata={
                "source": "cloudwatch",
                "alarm_name": alarm_name,
                "state": state_value,
            },
            incident_id=str(incident_id),
        )
        task = run_background_chat.delay(
            user_id=user_id,
            session_id=session_id,
            initial_message=rca_prompt,
            trigger_metadata={
                "source": "cloudwatch",
                "alarm_name": alarm_name,
                "state": state_value,
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
            "[CLOUDWATCH][ALARM] Triggered RCA for session %s (task=%s)",
            session_id, task.id,
        )
    except Exception as exc:
        logger.exception("[CLOUDWATCH][ALARM] Failed to trigger RCA: %s", exc)


def _extract_alarm_fields(payload: Dict[str, Any]) -> AlarmFields:
    """Parse alarm field values from the CloudWatch/SNS payload."""
    return AlarmFields(
        alarm_name=payload.get("AlarmName") or "Unknown Alarm",
        alarm_arn=payload.get("AlarmArn") or payload.get("alarm_arn") or "",
        state_value=payload.get("NewStateValue") or payload.get("state_value") or "",
        previous_state=payload.get("OldStateValue") or "",
        reason=payload.get("NewStateReason") or payload.get("reason") or "",
        account_id=payload.get("AWSAccountId") or payload.get("account_id") or "",
        region=payload.get("Region") or payload.get("region") or "",
    )


def _process_alarm_in_db(
    user_id: str, payload: Dict[str, Any],
    metadata: Optional[Dict[str, Any]], skip_rca: bool,
) -> None:
    """Core DB logic for processing a CloudWatch alarm (called from Celery task)."""
    from utils.db.connection_pool import db_pool
    from utils.auth.stateless_auth import set_rls_context

    fields = _extract_alarm_fields(payload)

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            received_at = datetime.now(timezone.utc)
            org_id = set_rls_context(cursor, conn, user_id, log_prefix="[CLOUDWATCH][ALARM]")
            if not org_id:
                return

            alarm_db_id, was_inserted = _persist_alarm(
                cursor, conn, user_id, org_id, fields,
                payload, received_at,
                sns_message_id=(metadata or {}).get("sns_message_id", ""),
            )
            if not alarm_db_id:
                return

            if not was_inserted:
                logger.info("[CLOUDWATCH][ALARM] Duplicate SNS delivery, skipping processing")
                return

            if _is_alarm_resolved(fields.state_value):
                _handle_resolved_alarm(
                    cursor, conn, user_id, org_id, fields.alarm_name,
                    alarm_db_id, fields.state_value, payload,
                )
                return

            if not _is_alarm_firing(fields.state_value):
                logger.info(
                    "[CLOUDWATCH][ALARM] State=%s for user %s — not firing, skipping incident.",
                    fields.state_value, user_id,
                )
                return

            severity = _extract_severity(fields.state_value, payload)
            service = _extract_service(payload)
            alert_metadata = _build_alert_metadata(
                fields.alarm_name, fields.alarm_arn, fields.account_id,
                fields.region, fields.reason, fields.previous_state, payload,
            )

            if _try_correlate_alarm(
                cursor, conn, user_id, org_id, alarm_db_id,
                fields.alarm_name, service, severity, alert_metadata, payload,
            ):
                return

            incident_id = _create_incident_record(
                cursor, conn, user_id, org_id, alarm_db_id,
                fields.alarm_name, service, severity, alert_metadata, received_at,
            )
            if not incident_id:
                return

            logger.info(
                "[CLOUDWATCH][ALARM] Created incident %s for alarm '%s'",
                incident_id, fields.alarm_name,
            )

            _post_incident_actions(
                user_id, org_id, incident_id, fields.alarm_name,
                service, severity, fields.state_value, alert_metadata,
                payload, skip_rca, cursor, conn,
            )


@celery_app.task(
    bind=True, max_retries=3, default_retry_delay=30, name="cloudwatch.process_alarm"
)
def process_cloudwatch_alarm(
    self,
    payload: Dict[str, Any],
    metadata: Optional[Dict[str, Any]] = None,
    user_id: Optional[str] = None,
    skip_rca: bool = False,
) -> None:
    """Background processor for CloudWatch alarm notifications.

    Args:
        payload: Parsed SNS/CloudWatch JSON payload.
        metadata: Auxiliary HTTP context captured at the route layer.
        user_id: Aurora user ID this alarm belongs to.
        skip_rca: If True, store but do not trigger RCA.
    """
    try:
        fields = _extract_alarm_fields(payload)
        logger.info(
            "[CLOUDWATCH][ALARM][USER:%s] %s  state=%s -> %s",
            user_id or "unknown", fields.alarm_name, fields.previous_state, fields.state_value,
        )
        logger.debug("[CLOUDWATCH][ALARM] full payload=%s", _safe_json(payload))

        if not user_id:
            logger.warning("[CLOUDWATCH][ALARM] No user_id provided, alarm not stored")
            return

        _process_alarm_in_db(user_id, payload, metadata, skip_rca)

    except Exception as exc:
        logger.exception("[CLOUDWATCH][ALARM] Failed to process alarm payload")
        if user_id:
            raise self.retry(exc=exc) from exc
