"""Celery tasks for the Elastic (Kibana alert webhook) integration."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from celery_config import celery_app
from chat.background.rca_prompt_builder import build_rca_prompt
from services.correlation import apply_correlation_outcome
from services.correlation.alert_correlator import AlertCorrelator

logger = logging.getLogger(__name__)

SOURCE = "elastic"
RCA_PREFERENCE_KEY = "elastic_rca_enabled"
_SEVERITY_WORDS = ("critical", "high", "medium", "low")


def _should_trigger_background_chat(user_id: str) -> bool:
    from utils.auth.stateless_auth import get_user_preference

    rca_enabled = get_user_preference(user_id, RCA_PREFERENCE_KEY, default=False)
    if not rca_enabled:
        logger.debug("[ELASTIC] Skipping background RCA - %s disabled for user %s", RCA_PREFERENCE_KEY, user_id)
        return False
    return True


# --------------------------------------------------------------------------- #
# Payload normalisation
# --------------------------------------------------------------------------- #


def _first(payload: Dict[str, Any], *paths: str) -> Any:
    """Return the first non-empty value among dotted paths (``context.reason``)."""
    for path in paths:
        current: Any = payload
        for part in path.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                current = None
                break
        if current not in (None, ""):
            return current
    return None


def _parse_tags(raw: Any) -> List[str]:
    """``rule_tags`` may be a list, ``"a,b"``, or a JSON-array string."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(t).strip() for t in raw if str(t).strip()]
    text = str(raw).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(t).strip() for t in parsed if str(t).strip()]
        except ValueError:
            pass  # not a JSON array: fall through to comma-separated parsing
    return [t.strip() for t in text.split(",") if t.strip()]


def normalize_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Map our Kibana action-body template (or raw ``context``/``rule`` objects) to flat keys."""
    rule_id = _first(payload, "rule_id", "rule.id", "ruleId")
    rule_name = _first(payload, "rule_name", "rule.name", "ruleName")
    rule_type = _first(payload, "rule_type", "rule.type", "ruleType")
    alert_id = _first(payload, "alert_id", "alert.id", "alertId")
    alert_uuid = _first(payload, "alert_uuid", "alert.uuid", "alertUuid")
    action_group = _first(payload, "action_group", "alert.actionGroup", "actionGroup")
    tags = _parse_tags(_first(payload, "rule_tags", "rule.tags", "tags"))

    # Dedupe key. Only synthesise a fallback when at least one identifier is
    # present — a body with neither would collapse every alert in the org onto
    # the constant "rule:alert" and fold them all into one incident.
    alert_uuid_str = str(alert_uuid)[:255] if alert_uuid else None
    if not alert_uuid_str and (rule_id or alert_id):
        alert_uuid_str = f"{rule_id or 'rule'}:{alert_id or 'alert'}"[:255]

    action_group_str = str(action_group).strip().lower()[:100] if action_group else ""
    alert_state = "recovered" if action_group_str == "recovered" else "active"

    reason = _first(payload, "reason", "context.reason", "message", "context.message")
    # Prefer the rule name: Stack rules render context.title verbosely
    # ("rule 'X' matched query"); Observability rules have no title at all.
    title = rule_name or _first(payload, "title", "context.title") or "Kibana Alert"

    # Column widths: alert_id/rule_id/rule_type VARCHAR(255), action_group VARCHAR(100).
    # Kibana renders {{alert.id}} as the joined group key, which has no length cap;
    # the full value is preserved in the raw payload column.
    return {
        "rule_id": str(rule_id)[:255] if rule_id else None,
        "rule_name": str(rule_name) if rule_name else None,
        "rule_type": str(rule_type)[:255] if rule_type else None,
        "rule_url": _first(payload, "rule_url", "rule.url"),
        "space_id": _first(payload, "space_id", "rule.spaceId", "spaceId"),
        "alert_id": str(alert_id)[:255] if alert_id else None,
        "alert_uuid": alert_uuid_str,
        "action_group": action_group_str or None,
        "action_group_name": _first(payload, "action_group_name", "alert.actionGroupName"),
        "alert_state": alert_state,
        "tags": tags,
        "reason": str(reason)[:5000] if reason else None,
        "title": str(title)[:500],
        "value": _first(payload, "value", "context.value"),
        "threshold": _first(payload, "threshold", "context.threshold"),
        "group": _first(payload, "group", "context.group"),
        "timestamp": _first(payload, "timestamp", "context.timestamp", "date"),
        "kibana_url": _first(payload, "kibana_url", "kibanaBaseUrl", "kibana_base_url"),
        "view_in_app_url": _first(payload, "view_in_app_url", "context.viewInAppUrl", "viewInAppUrl"),
        "alert_details_url": _first(payload, "alert_details_url", "context.alertDetailsUrl", "alertDetailsUrl"),
        "hits": _first(payload, "hits", "context.hits"),
    }


def _extract_severity(tags: List[str]) -> str:
    for tag in tags:
        lowered = tag.lower()
        if lowered.startswith("severity:"):
            lowered = lowered.split(":", 1)[1].strip()
        if lowered in _SEVERITY_WORDS:
            return lowered
        if lowered in ("sev1", "p1"):
            return "critical"
        if lowered in ("sev2", "p2"):
            return "high"
    return "unknown"


def _extract_service(normalized: Dict[str, Any]) -> str:
    group = normalized.get("group")
    if group:
        return str(group)[:255]
    for tag in normalized.get("tags") or []:
        if tag.lower().startswith("service:"):
            value = tag.split(":", 1)[1].strip()
            if value:
                return value[:255]
    return (normalized.get("rule_name") or "unknown")[:255]


def _build_alert_metadata(normalized: Dict[str, Any]) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {}
    mapping = {
        "ruleId": "rule_id",
        "ruleName": "rule_name",
        "ruleType": "rule_type",
        "reason": "reason",
        "value": "value",
        "threshold": "threshold",
        "viewInAppUrl": "view_in_app_url",
        "alertDetailsUrl": "alert_details_url",
        "kibanaUrl": "kibana_url",
        "spaceId": "space_id",
        "alertUuid": "alert_uuid",
        "actionGroup": "action_group",
    }
    for out_key, in_key in mapping.items():
        value = normalized.get(in_key)
        if value not in (None, ""):
            metadata[out_key] = value
    if normalized.get("tags"):
        metadata["tags"] = normalized["tags"]
    return metadata


def _safe_json_dump(data: Dict[str, Any]) -> str:
    try:
        return json.dumps(data, ensure_ascii=False, default=str)
    except Exception:
        return str(data)


# --------------------------------------------------------------------------- #
# DB helpers
# --------------------------------------------------------------------------- #


def _insert_alert_row(cursor, user_id, org_id, normalized, severity, payload, received_at) -> Optional[int]:
    cursor.execute(
        """
        INSERT INTO elastic_alerts
        (user_id, org_id, alert_id, alert_uuid, alert_title, alert_state, rule_id, rule_name,
         rule_type, action_group, reason, severity, view_in_app_url, payload, received_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            user_id,
            org_id,
            normalized["alert_id"],
            normalized["alert_uuid"],
            normalized["title"],
            normalized["alert_state"],
            normalized["rule_id"],
            normalized["rule_name"],
            normalized["rule_type"],
            normalized["action_group"],
            normalized["reason"],
            severity,
            normalized["view_in_app_url"],
            json.dumps(payload, default=str),
            received_at,
        ),
    )
    row = cursor.fetchone()
    return row[0] if row else None


def _find_open_incident_for_alert(cursor, org_id: str, alert_uuid: str) -> Optional[tuple]:
    """Return ``(elastic_alert_id, incident_id)`` for an active alert already linked to an OPEN incident.

    Resolved (and recurrence-folded ``merged``) incidents are excluded so a
    re-fire after a human resolved the incident starts a new investigation
    instead of silently bumping the closed one — mirrors the status gate the
    AlertCorrelator applies to its own candidates.
    """
    cursor.execute(
        """
        SELECT ea.id, ia.incident_id
        FROM elastic_alerts ea
        JOIN incident_alerts ia
          ON ia.source_type = %s AND ia.source_alert_id = ea.id
        JOIN incidents i
          ON i.id = ia.incident_id AND i.status NOT IN ('resolved', 'merged')
        WHERE ea.org_id = %s AND ea.alert_uuid = %s AND ea.alert_state = 'active'
        ORDER BY ea.received_at DESC
        LIMIT 1
        """,
        (SOURCE, org_id, alert_uuid),
    )
    return cursor.fetchone()


# --------------------------------------------------------------------------- #
# Task
# --------------------------------------------------------------------------- #


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30, name="elastic.process_alert")
def process_elastic_alert(
    self,
    payload: Dict[str, Any],
    metadata: Optional[Dict[str, Any]] = None,
    user_id: Optional[str] = None,
) -> None:
    """Background processor for Kibana alert webhooks."""
    try:
        normalized = normalize_payload(payload if isinstance(payload, dict) else {"raw": payload})
        logger.info(
            "[ELASTIC][ALERT][USER:%s] %s (%s, uuid=%s)",
            user_id or "unknown", normalized["title"], normalized["alert_state"], normalized["alert_uuid"],
        )
        logger.debug(
            "[ELASTIC][ALERT] full payload=%s",
            _safe_json_dump({"payload": payload, "metadata": metadata or {}, "user_id": user_id}),
        )

        if not user_id:
            logger.warning("[ELASTIC][ALERT] No user_id provided, alert not stored in database")
            return

        from utils.auth.stateless_auth import set_rls_context
        from utils.db.connection_pool import db_pool

        try:
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cursor:
                    org_id = set_rls_context(cursor, conn, user_id, log_prefix="[ELASTIC][ALERT]")
                    if not org_id:
                        return
                    _process_with_cursor(cursor, conn, user_id, org_id, normalized, payload)
        except Exception as db_exc:
            logger.exception("[ELASTIC][ALERT] Failed to store alert in database: %s", db_exc)

    except Exception as exc:
        logger.exception("[ELASTIC][ALERT] Failed to process alert payload")
        raise self.retry(exc=exc)


def _process_with_cursor(cursor, conn, user_id: str, org_id: str, normalized: Dict[str, Any], payload: Dict[str, Any]) -> None:
    received_at = datetime.now(timezone.utc)
    severity = _extract_severity(normalized["tags"])
    alert_uuid = normalized["alert_uuid"]
    alert_title = normalized["title"]

    if not alert_uuid:
        logger.warning(
            "[ELASTIC][ALERT] Webhook body for user %s carries no alert.uuid / rule.id / alert.id; "
            "storing without dedupe (paste the Aurora action body template into the Kibana rule)",
            user_id,
        )

    # --- Recovery: store, mark previous active rows recovered, no incident ---
    if normalized["alert_state"] == "recovered":
        alert_db_id = _insert_alert_row(cursor, user_id, org_id, normalized, severity, payload, received_at)
        if alert_uuid:
            cursor.execute(
                """
                UPDATE elastic_alerts SET alert_state = 'recovered'
                WHERE org_id = %s AND alert_uuid = %s AND alert_state = 'active' AND id <> %s
                """,
                (org_id, alert_uuid, alert_db_id),
            )
        conn.commit()
        logger.info("[ELASTIC][ALERT] Recorded recovery for %s (row %s)", alert_uuid, alert_db_id)
        return

    # --- Dedupe: Kibana re-fires every interval while the alert stays active ---
    existing = _find_open_incident_for_alert(cursor, org_id, alert_uuid) if alert_uuid else None
    if existing:
        existing_alert_id, incident_id = existing
        cursor.execute(
            "UPDATE elastic_alerts SET payload = %s, received_at = %s WHERE id = %s",
            (json.dumps(payload, default=str), received_at, existing_alert_id),
        )
        cursor.execute("UPDATE incidents SET updated_at = CURRENT_TIMESTAMP WHERE id = %s", (incident_id,))
        conn.commit()
        logger.info(
            "[ELASTIC][ALERT] Re-fire for active alert %s → refreshed row %s / incident %s (no new incident)",
            alert_uuid, existing_alert_id, incident_id,
        )
        return

    alert_db_id = _insert_alert_row(cursor, user_id, org_id, normalized, severity, payload, received_at)
    if not alert_db_id:
        conn.rollback()
        logger.error("[ELASTIC][ALERT] Failed to get alert_id for user %s", user_id)
        return

    service = _extract_service(normalized)
    alert_metadata = _build_alert_metadata(normalized)
    rca_enabled = _should_trigger_background_chat(user_id)

    try:
        correlator = AlertCorrelator()
        correlation_result = correlator.correlate(
            cursor=cursor,
            user_id=user_id,
            source_type=SOURCE,
            source_alert_id=alert_db_id,
            alert_title=alert_title,
            alert_service=service,
            alert_severity=severity,
            alert_metadata=alert_metadata,
            org_id=org_id,
        )
        if correlation_result.is_correlated and apply_correlation_outcome(
            cursor=cursor,
            user_id=user_id,
            incident_id=correlation_result.incident_id,
            source_type=SOURCE,
            source_alert_id=alert_db_id,
            alert_title=alert_title,
            alert_service=service,
            alert_severity=severity,
            correlation_result=correlation_result,
            alert_metadata=alert_metadata,
            raw_payload=payload,
            org_id=org_id,
            hint_only_eligible=rca_enabled,
        ):
            conn.commit()
            return
    except Exception as corr_exc:
        logger.warning("[ELASTIC] Correlation check failed, proceeding with normal flow: %s", corr_exc)

    if not rca_enabled:
        conn.commit()
        logger.info(
            "[ELASTIC][ALERT] Stored alert %s for user %s (RCA disabled, no incident created)",
            alert_db_id, user_id,
        )
        return

    cursor.execute(
        """
        INSERT INTO incidents
        (user_id, org_id, source_type, source_alert_id, alert_title, alert_service,
         severity, status, started_at, alert_metadata)
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
            user_id, org_id, SOURCE, alert_db_id, alert_title, service,
            severity, "investigating", received_at, json.dumps(alert_metadata, default=str),
        ),
    )
    incident_row = cursor.fetchone()
    incident_id = incident_row[0] if incident_row else None
    conn.commit()
    logger.info("[ELASTIC][ALERT] Stored alert and incident in database for user %s", user_id)

    try:
        cursor.execute(
            """INSERT INTO incident_alerts
               (user_id, org_id, incident_id, source_type, source_alert_id, alert_title, alert_service,
                alert_severity, correlation_strategy, correlation_score, alert_metadata)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                user_id, org_id, incident_id, SOURCE, alert_db_id, alert_title, service,
                severity, "primary", 1.0, json.dumps(alert_metadata, default=str),
            ),
        )
        cursor.execute(
            "UPDATE incidents SET affected_services = ARRAY[%s] WHERE id = %s",
            (service, incident_id),
        )
        conn.commit()
    except Exception as e:
        logger.warning("[ELASTIC] Failed to record primary alert: %s", e)

    if not incident_id:
        return

    logger.info("[ELASTIC][ALERT] Created incident %s for alert %s", incident_id, alert_db_id)

    from chat.background.summarization import generate_incident_summary

    generate_incident_summary.delay(
        incident_id=str(incident_id),
        user_id=user_id,
        source_type=SOURCE,
        alert_title=alert_title,
        severity=severity,
        service=service,
        raw_payload=payload,
        alert_metadata=alert_metadata,
    )

    try:
        from chat.background.task import (
            create_background_chat_session,
            is_background_chat_allowed,
            run_background_chat,
        )

        if not is_background_chat_allowed(user_id):
            logger.info("[ELASTIC][ALERT] Skipping background RCA - rate limited for user %s", user_id)
            return

        session_id = create_background_chat_session(
            user_id=user_id,
            title=f"RCA: {alert_title}",
            trigger_metadata={
                "source": SOURCE,
                "alert_id": normalized["alert_id"],
                "alert_uuid": alert_uuid,
                "rule_name": normalized["rule_name"],
            },
            incident_id=str(incident_id),
        )
        rca_prompt, rail_text = build_rca_prompt(SOURCE, alert_title, payload, user_id=user_id)
        task = run_background_chat.delay(
            user_id=user_id,
            session_id=session_id,
            initial_message=rca_prompt,
            trigger_metadata={
                "source": SOURCE,
                "alert_id": normalized["alert_id"],
                "alert_uuid": alert_uuid,
                "alert_title": alert_title,
            },
            incident_id=str(incident_id),
            rail_text=rail_text,
        )
        cursor.execute(
            "UPDATE incidents SET rca_celery_task_id = %s WHERE id = %s",
            (task.id, str(incident_id)),
        )
        conn.commit()
        logger.info("[ELASTIC][ALERT] Triggered background RCA chat for session %s (task_id=%s)", session_id, task.id)
    except Exception as chat_exc:
        logger.exception("[ELASTIC][ALERT] Failed to trigger background chat: %s", chat_exc)
