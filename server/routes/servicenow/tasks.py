"""Background Celery task for ServiceNow incident webhooks.

A Business Rule on the customer's Incident table POSTs the record to
``/servicenow/webhook/<user_id>``; this task stores the raw event, creates
(or updates) the Aurora incident and starts the RCA pipeline, the same flow
as the Jira and PagerDuty connectors.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from celery_config import celery_app
from chat.background.rca_prompt_builder import build_rca_prompt
from routes.servicenow.servicenow_client import incident_url
from services.correlation.alert_correlator import AlertCorrelator
from services.correlation import apply_correlation_outcome
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

SOURCE = "servicenow"
_LOG = "[SERVICENOW][WEBHOOK]"

# Per-user RCA preferences (read by the routes and by this task).
PREF_RCA_ENABLED = "servicenow_rca_enabled"
PREF_MIN_PRIORITY = "servicenow_min_priority"
PREF_WORK_NOTES = "servicenow_work_notes_enabled"
DEFAULT_MIN_PRIORITY = 5  # 5 = investigate every priority

_PRIORITY_RE = re.compile(r"[1-5]")
# ServiceNow incident states: 1 New, 2 In Progress, 3 On Hold, 6 Resolved, 7 Closed, 8 Canceled
_RESOLVED_STATE_VALUES = frozenset({"6", "7", "8"})
_RESOLVED_STATE_LABELS = frozenset({"resolved", "closed", "canceled", "cancelled"})
_SEVERITY_BY_PRIORITY = {1: "critical", 2: "high", 3: "medium", 4: "low", 5: "low"}
_SN_DATETIME = "%Y-%m-%d %H:%M:%S"


# ── pure helpers (unit tested) ───────────────────────────────────────


def priority_number(value: Any) -> Optional[int]:
    """1-5 from a ServiceNow priority value ("2") or label ("2 - High"); None when absent."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = int(value)
        return number if 1 <= number <= 5 else None
    match = _PRIORITY_RE.search(str(value))
    return int(match.group(0)) if match else None


def incident_priority(payload: Dict[str, Any]) -> Optional[int]:
    return priority_number(payload.get("priority")) or priority_number(payload.get("priority_label"))


def extract_severity(payload: Dict[str, Any]) -> str:
    """Aurora severity from the incident priority (unknown priority = medium)."""
    return _SEVERITY_BY_PRIORITY.get(incident_priority(payload) or 0, "medium")


def is_resolved(payload: Dict[str, Any]) -> bool:
    value = str(payload.get("state") or "").strip()
    label = str(payload.get("state_label") or "").strip().lower()
    return value in _RESOLVED_STATE_VALUES or label in _RESOLVED_STATE_LABELS


def priority_passes(priority: Optional[int], min_priority: int) -> bool:
    """RCA runs for priorities at or more urgent than ``min_priority`` (1 = most urgent).

    An incident with no priority is investigated only when everything is (5).
    """
    if priority is None:
        return min_priority >= 5
    return priority <= min_priority


def get_rca_settings(user_id: str) -> Dict[str, Any]:
    from utils.auth.stateless_auth import get_user_preference

    enabled = get_user_preference(user_id, PREF_RCA_ENABLED, default=True)
    min_priority = priority_number(
        get_user_preference(user_id, PREF_MIN_PRIORITY, default=DEFAULT_MIN_PRIORITY)
    ) or DEFAULT_MIN_PRIORITY
    work_notes = get_user_preference(user_id, PREF_WORK_NOTES, default=False)
    return {
        "rcaEnabled": enabled is not False,
        "minPriority": min_priority,
        "workNotesEnabled": work_notes is True,
    }


def parse_servicenow_datetime(value: Any) -> Optional[datetime]:
    """ServiceNow API timestamps are 'YYYY-MM-DD HH:MM:SS' in UTC; ISO 8601 is accepted too."""
    if not value:
        return None
    text = str(value).strip()
    try:
        return datetime.strptime(text, _SN_DATETIME).replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def alert_title(payload: Dict[str, Any]) -> str:
    number = str(payload.get("number") or "").strip()
    summary = str(payload.get("short_description") or "").strip() or "ServiceNow incident"
    title = f"[{number}] {summary}" if number else summary
    return title[:500]


def alert_service(payload: Dict[str, Any]) -> str:
    for key in ("cmdb_ci", "assignment_group", "category"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value[:255]
    return "servicenow"


def build_alert_metadata(payload: Dict[str, Any], instance_url: Optional[str]) -> Dict[str, Any]:
    sys_id = str(payload.get("sys_id") or "")
    meta: Dict[str, Any] = {
        "sysId": sys_id,
        "number": payload.get("number") or "",
        "priority": payload.get("priority_label") or payload.get("priority") or "",
        "impact": payload.get("impact") or "",
        "urgency": payload.get("urgency") or "",
        "state": payload.get("state_label") or payload.get("state") or "",
        "category": payload.get("category") or "",
        "assignmentGroup": payload.get("assignment_group") or "",
        "cmdbCi": payload.get("cmdb_ci") or "",
        "caller": payload.get("caller") or "",
        "openedAt": payload.get("opened_at") or "",
        "event": str(payload.get("event") or "inserted").lower(),
    }
    if instance_url and sys_id:
        meta["incidentUrl"] = incident_url(instance_url, sys_id)
    description = payload.get("description")
    if description:
        meta["description"] = str(description)[:2000]
    return {key: value for key, value in meta.items() if value not in ("", None)}


def source_alert_id_for(org_id: str, sys_id: str) -> int:
    """incidents.source_alert_id is an integer: stable hash of the record sys_id."""
    digest = hashlib.sha256(f"{org_id}:{sys_id}".encode()).hexdigest()
    return int(digest[:8], 16) % (2**31)


# ── the task ─────────────────────────────────────────────────────────


@celery_app.task(
    bind=True, max_retries=3, default_retry_delay=30, name="servicenow.process_event"
)
def process_servicenow_event(
    self,
    payload: Dict[str, Any],
    user_id: Optional[str] = None,
) -> None:
    """Store the event, upsert the Aurora incident and dispatch the RCA."""
    payload = payload or {}
    sys_id = str(payload.get("sys_id") or "")
    number = str(payload.get("number") or "")

    logger.info("%s Processing event user=%s number=%s", _LOG, user_id or "unknown", sanitize(number))

    try:
        if not user_id or not sys_id:
            logger.warning("%s Missing user_id or sys_id; skipping", _LOG)
            return

        from utils.auth.stateless_auth import set_rls_context
        from utils.auth.token_management import get_token_data
        from utils.db.connection_pool import db_pool

        creds = get_token_data(user_id, SOURCE) or {}
        instance_url = creds.get("instance_url")

        event_kind = str(payload.get("event") or "inserted").lower()
        severity = extract_severity(payload)
        service = alert_service(payload)
        title = alert_title(payload)
        metadata = build_alert_metadata(payload, instance_url)
        status = "resolved" if is_resolved(payload) else "investigating"

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                org_id = set_rls_context(cursor, conn, user_id, log_prefix=_LOG)
                if not org_id:
                    return

                received_at = datetime.now(timezone.utc)
                _store_event(cursor, conn, user_id, org_id, payload, event_kind, title, status, severity, received_at)

                alert_fired_at = parse_servicenow_datetime(payload.get("opened_at"))
                source_alert_id = source_alert_id_for(org_id, sys_id)

                if event_kind == "inserted" and status != "resolved" and _try_correlate(
                    cursor, conn, user_id, org_id, source_alert_id,
                    title, service, severity, metadata, payload,
                ):
                    return

                cursor.execute(
                    """
                    INSERT INTO incidents
                    (user_id, org_id, source_type, source_alert_id, alert_title, alert_service,
                     severity, status, started_at, alert_metadata, alert_fired_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (org_id, source_type, source_alert_id, user_id) DO UPDATE
                    SET updated_at = CURRENT_TIMESTAMP,
                        alert_metadata = EXCLUDED.alert_metadata,
                        status = CASE WHEN EXCLUDED.status = 'resolved' THEN 'resolved'
                                      ELSE incidents.status END
                    RETURNING id, (xmax = 0) AS inserted
                    """,
                    (
                        user_id, org_id, SOURCE, source_alert_id, title, service,
                        severity, status, received_at, json.dumps(metadata), alert_fired_at,
                    ),
                )
                incident_row = cursor.fetchone()
                incident_id = incident_row[0] if incident_row else None
                incident_was_inserted = bool(incident_row[1]) if incident_row else False
                conn.commit()

                if not incident_id:
                    logger.error("%s Failed to create incident for %s", _LOG, sanitize(number))
                    return

                if not incident_was_inserted:
                    # Update event (or duplicate delivery): the row is refreshed, the
                    # RCA already ran or is running; never dispatch a second one.
                    logger.info(
                        "%s Updated existing incident %s for %s (event=%s, status=%s)",
                        _LOG, incident_id, sanitize(number), event_kind, status,
                    )
                    _notify_sse(user_id, org_id, incident_id)
                    return

                _record_primary_alert(
                    cursor, conn, user_id, org_id, incident_id, source_alert_id,
                    title, service, severity, metadata,
                )
                _record_lifecycle_event(cursor, conn, user_id, org_id, incident_id)
                logger.info("%s Created incident %s for %s", _LOG, incident_id, sanitize(number))

                _notify_and_summarize(
                    user_id, org_id, incident_id, title, severity, service, payload, metadata,
                )

                if status == "resolved":
                    logger.info("%s Incident %s arrived already resolved; no RCA", _LOG, incident_id)
                    return
                settings = get_rca_settings(user_id)
                if not settings["rcaEnabled"]:
                    logger.info("%s RCA disabled for user %s; skipping", _LOG, user_id)
                    return
                priority = incident_priority(payload)
                if not priority_passes(priority, settings["minPriority"]):
                    logger.info(
                        "%s Priority %s below minimum %s; skipping RCA for %s",
                        _LOG, priority, settings["minPriority"], sanitize(number),
                    )
                    return
                _dispatch_rca(cursor, conn, user_id, incident_id, title, number, sys_id, payload)

    except Exception as exc:
        logger.exception("%s Failed to process event", _LOG)
        raise self.retry(exc=exc)


# ── pipeline steps ───────────────────────────────────────────────────


def _store_event(
    cursor, conn, user_id: str, org_id: str, payload: Dict[str, Any], event_kind: str,
    title: str, status: str, severity: str, received_at: datetime,
) -> None:
    """Keep the raw payload (get_alert_field reads it back during the RCA). Best effort."""
    try:
        cursor.execute("SAVEPOINT sp_servicenow_event")
        cursor.execute(
            """INSERT INTO servicenow_events
               (user_id, org_id, event_type, sys_id, number, incident_title,
                incident_status, severity, payload, received_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                user_id, org_id, event_kind[:50], str(payload.get("sys_id") or "")[:64],
                str(payload.get("number") or "")[:64], title[:500], status, severity,
                json.dumps(payload), received_at,
            ),
        )
        cursor.execute("RELEASE SAVEPOINT sp_servicenow_event")
        conn.commit()
    except Exception as exc:
        try:
            cursor.execute("ROLLBACK TO SAVEPOINT sp_servicenow_event")
        except Exception as rb_exc:
            logger.debug("%s Savepoint rollback failed: %s", _LOG, rb_exc)
        logger.warning("%s Failed to store raw event: %s", _LOG, exc)


def _record_primary_alert(
    cursor, conn, user_id: str, org_id: str, incident_id, source_alert_id: int,
    title: str, service: str, severity: str, metadata: Dict[str, Any],
) -> None:
    try:
        cursor.execute(
            """INSERT INTO incident_alerts
               (user_id, org_id, incident_id, source_type, source_alert_id, alert_title,
                alert_service, alert_severity, correlation_strategy, correlation_score, alert_metadata)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                user_id, org_id, incident_id, SOURCE, source_alert_id,
                title, service, severity, "primary", 1.0, json.dumps(metadata),
            ),
        )
        cursor.execute(
            "UPDATE incidents SET affected_services = ARRAY[%s] WHERE id = %s",
            (service, incident_id),
        )
        conn.commit()
    except Exception:
        # Re-raise so Celery retries: an incident without its primary alert row is inconsistent.
        logger.exception("%s Failed to record primary alert for incident %s", _LOG, incident_id)
        raise


def _record_lifecycle_event(cursor, conn, user_id: str, org_id: str, incident_id) -> None:
    try:
        cursor.execute("SAVEPOINT sp_servicenow_lifecycle")
        cursor.execute(
            """INSERT INTO incident_lifecycle_events
               (incident_id, user_id, org_id, event_type, new_value)
               VALUES (%s, %s, %s, %s, %s)""",
            (incident_id, user_id, org_id, "created", "investigating"),
        )
        cursor.execute("RELEASE SAVEPOINT sp_servicenow_lifecycle")
        conn.commit()
    except Exception as exc:
        try:
            cursor.execute("ROLLBACK TO SAVEPOINT sp_servicenow_lifecycle")
        except Exception as rb_exc:
            logger.debug("%s Savepoint rollback failed: %s", _LOG, rb_exc)
        logger.warning("%s Failed to record lifecycle event: %s", _LOG, exc)


def _notify_sse(user_id: str, org_id: str, incident_id) -> None:
    try:
        from routes.incidents_sse import broadcast_incident_update_to_user_connections

        broadcast_incident_update_to_user_connections(
            user_id,
            {"type": "incident_update", "incident_id": str(incident_id), "source": SOURCE},
            org_id=org_id,
        )
    except Exception as exc:
        logger.warning("%s Failed to notify SSE: %s", _LOG, exc)


def _notify_and_summarize(
    user_id: str, org_id: str, incident_id, title: str, severity: str,
    service: str, payload: Dict[str, Any], metadata: Dict[str, Any],
) -> None:
    _notify_sse(user_id, org_id, incident_id)

    from chat.background.summarization import generate_incident_summary

    generate_incident_summary.delay(
        incident_id=str(incident_id),
        user_id=user_id,
        source_type=SOURCE,
        alert_title=title,
        severity=severity,
        service=service,
        raw_payload=payload,
        alert_metadata=metadata,
    )


def _try_correlate(
    cursor, conn, user_id: str, org_id: str, source_alert_id: int,
    title: str, service: str, severity: str,
    metadata: Dict[str, Any], payload: Dict[str, Any],
) -> bool:
    """Attach this incident to an open Aurora incident when it correlates. True = stop here."""
    try:
        correlator = AlertCorrelator()
        correlation_result = correlator.correlate(
            cursor=cursor,
            user_id=user_id,
            source_type=SOURCE,
            source_alert_id=source_alert_id,
            alert_title=title,
            alert_service=service,
            alert_severity=severity,
            alert_metadata=metadata,
            org_id=org_id,
        )
        if not correlation_result.is_correlated:
            return False
        if not apply_correlation_outcome(
            cursor=cursor,
            user_id=user_id,
            incident_id=correlation_result.incident_id,
            source_type=SOURCE,
            source_alert_id=source_alert_id,
            alert_title=title,
            alert_service=service,
            alert_severity=severity,
            correlation_result=correlation_result,
            alert_metadata=metadata,
            raw_payload=payload,
            org_id=org_id,
        ):
            return False
        conn.commit()
        return True
    except Exception as exc:
        logger.warning("%s Correlation check failed: %s", _LOG, exc)
        return False


def _dispatch_rca(
    cursor, conn, user_id: str, incident_id, title: str,
    number: str, sys_id: str, payload: Dict[str, Any],
) -> None:
    """Kick off the background RCA chat for a freshly created incident."""
    session_id = None
    task = None
    trigger_metadata = {"source": SOURCE, "number": number, "sys_id": sys_id}
    try:
        from chat.background.task import (
            create_background_chat_session,
            is_background_chat_allowed,
            run_background_chat,
        )

        if not is_background_chat_allowed(user_id):
            logger.info("%s Skipping RCA - rate limited for user %s", _LOG, user_id)
            return

        session_id = create_background_chat_session(
            user_id=user_id,
            title=f"RCA: {title}",
            trigger_metadata=trigger_metadata,
            incident_id=str(incident_id),
        )

        rca_prompt, rail_text = build_rca_prompt(SOURCE, title, payload, user_id=user_id)

        task = run_background_chat.delay(
            user_id=user_id,
            session_id=session_id,
            initial_message=rca_prompt,
            trigger_metadata=trigger_metadata,
            incident_id=str(incident_id),
            rail_text=rail_text,
        )

        cursor.execute(
            "UPDATE incidents SET rca_celery_task_id = %s WHERE id = %s",
            (task.id, str(incident_id)),
        )
        conn.commit()
        logger.info("%s Triggered RCA for %s session=%s task=%s", _LOG, sanitize(number), session_id, task.id)
    except Exception:
        logger.exception("%s Failed to trigger RCA", _LOG)
        if session_id and task is None:
            try:
                cursor.execute(
                    "UPDATE chat_sessions SET status = %s WHERE id = %s",
                    ("failed", session_id),
                )
                conn.commit()
            except Exception:
                logger.debug("%s Failed to mark RCA session failed", _LOG, exc_info=True)
