"""Whether a repeat of an existing incident needs another RCA.

Metric samples and templated annotation text are not part of the identity.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple, Optional

logger = logging.getLogger(__name__)

# Prefix written into rca_celery_task_id while a request is enqueueing.
# The value is "pending:<utc timestamp>" so a later upsert, which always
# touches updated_at, cannot keep a dead claim looking fresh.
RCA_ENQUEUE_CLAIM = "pending"
_CLAIM_TTL = timedelta(minutes=2)


class ExistingIncident(NamedTuple):
    status: Optional[str]
    title: Optional[str]
    service: Optional[str]
    severity: Optional[str]
    metadata: Any
    session_id: Any
    task_id: Any


def metadata_labels(metadata) -> dict:
    """Label map from alert metadata. Values and annotation text are ignored."""
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            return {}
    if not isinstance(metadata, dict):
        return {}
    labels = metadata.get("labels") or {}
    return labels if isinstance(labels, dict) else {}


def alert_signature(title, service, severity, labels=None) -> tuple:
    """Stable problem identity for one alert."""
    if not isinstance(labels, dict):
        labels = {}
    label_items = tuple(
        sorted((str(key), "" if value is None else str(value)) for key, value in labels.items())
    )
    return (
        (title or "").strip(),
        (service or "").strip(),
        (severity or "").strip().lower(),
        label_items,
    )


def enqueue_claim(now=None) -> str:
    """Claim value whose age survives later updates to the incident row.

    The token changes on every retry, so a worker started for an older claim
    cannot adopt the new one.
    """
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return f"{RCA_ENQUEUE_CLAIM}:{uuid.uuid4().hex}:{current.isoformat()}"


def is_enqueue_claim(task_id) -> bool:
    """True when this task id is a claim, not a Celery id."""
    return isinstance(task_id, str) and (
        task_id == RCA_ENQUEUE_CLAIM or task_id.startswith(f"{RCA_ENQUEUE_CLAIM}:")
    )


def _claim_timestamp(task_id):
    """ISO timestamp stored after the claim prefix and optional generation token."""
    body = task_id[len(RCA_ENQUEUE_CLAIM):]
    if not body.startswith(":"):
        return None
    body = body[1:]
    # pending:<32 hex>:<timestamp>. A timestamp alone is the older format.
    if len(body) >= 33 and body[32] == ":" and all(c in "0123456789abcdef" for c in body[:32]):
        body = body[33:]
    return body or None


def _claim_time(task_id, now):
    """When the claim was taken. A claim with no timestamp is already expired."""
    raw = _claim_timestamp(task_id)
    # "pending" alone has no clock, so a crashed writer must not hold the incident forever.
    if not raw:
        return now - _CLAIM_TTL - timedelta(seconds=1)
    try:
        claimed_at = datetime.fromisoformat(raw)
    except ValueError:
        return now - _CLAIM_TTL - timedelta(seconds=1)
    if claimed_at.tzinfo is None:
        claimed_at = claimed_at.replace(tzinfo=timezone.utc)
    return claimed_at


def worker_owns_investigation(existing_task_id, request_id, claim) -> bool:
    """Whether this worker may link the incident and keep running.

    A retry writes a new claim. The worker started for the previous claim must stop.
    """
    if not existing_task_id or existing_task_id == request_id:
        return True
    # A different claim belongs to a newer investigation.
    return bool(claim) and existing_task_id == claim


def repeat_needs_investigation(
    previous_status,
    previous_signature,
    new_signature,
    *,
    session_id=None,
    task_id=None,
    now=None,
) -> bool:
    """Whether this firing of an existing incident should start another RCA."""
    # Resolved incident firing again is a regression, not a duplicate delivery.
    if (previous_status or "").lower() == "resolved":
        return True
    # Title, service, severity, or labels changed under the same alert id.
    if previous_signature != new_signature:
        return True
    # The insert committed, then the process died before RCA was enqueued.
    if session_id or (previous_status or "").lower() != "investigating":
        return False
    if not task_id:
        return True
    # Another request holds the enqueue claim. Retry only after it goes stale.
    if is_enqueue_claim(task_id):
        current = now or datetime.now(timezone.utc)
        return current - _claim_time(task_id, current) > _CLAIM_TTL
    return False


def should_start_investigation(was_inserted, existing: Optional[ExistingIncident], *, title, service, severity, labels=None) -> bool:
    """True for a new incident, a regression, a changed alert, or a lost enqueue."""
    if was_inserted:
        return True
    if existing is None:
        return False
    # Callers that don't pass labels compare title, service, and severity only.
    previous_labels = metadata_labels(existing.metadata) if labels is not None else {}
    compared_labels = labels if labels is not None else {}
    return repeat_needs_investigation(
        existing.status,
        alert_signature(existing.title, existing.service, existing.severity, previous_labels),
        alert_signature(title, service, severity, compared_labels),
        session_id=existing.session_id,
        task_id=existing.task_id,
    )


def lock_existing_incident(cursor, *, org_id, source_type, source_alert_id, user_id) -> Optional[ExistingIncident]:
    """Lock the incident this alert already owns, if any.

    Held until the caller commits, so two deliveries cannot both decide to investigate.
    """
    if source_alert_id is None:
        return None
    cursor.execute(
        """
        SELECT status, alert_title, alert_service, severity, alert_metadata,
               aurora_chat_session_id, rca_celery_task_id
        FROM incidents
        WHERE org_id = %s AND source_type = %s
          AND source_alert_id = %s AND user_id = %s
        FOR UPDATE
        """,
        (org_id, source_type, source_alert_id, user_id),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return ExistingIncident(*row)


def reopen_incident(cursor, *, incident_id, user_id, org_id, severity, title, service, started_at, previous_status, record_lifecycle=True) -> str:
    """Hand the incident's chat to a new RCA and claim the enqueue.

    The caller replaces rca_celery_task_id with the Celery id after delay().
    Returns the claim this retry owns.
    """
    claim = enqueue_claim()
    cursor.execute(
        """
        UPDATE incidents
           SET status = 'investigating',
               analyzed_at = NULL,
               aurora_status = 'idle',
               aurora_chat_session_id = NULL,
               rca_celery_task_id = %s,
               severity = %s,
               alert_title = %s,
               alert_service = %s,
               started_at = %s,
               updated_at = CURRENT_TIMESTAMP
         WHERE id = %s
        """,
        (claim, severity, title, service, started_at, incident_id),
    )
    # Already investigating — don't add a no-op timeline row.
    if record_lifecycle and (previous_status or "").lower() != "investigating":
        cursor.execute(
            """
            INSERT INTO incident_lifecycle_events
                (incident_id, user_id, org_id, event_type, previous_value, new_value)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (incident_id, user_id, org_id, "status_changed", previous_status, "investigating"),
        )
    return claim


def claim_enqueue(cursor, incident_id) -> str:
    """Claim a brand-new incident before its insert commits.

    The Celery id is written later, after summary and session setup. Without
    this, a second delivery in that window sees no task id and starts another RCA.
    """
    claim = enqueue_claim()
    cursor.execute(
        "UPDATE incidents SET rca_celery_task_id = %s WHERE id = %s",
        (claim, incident_id),
    )
    return claim


def replace_claim_with_task(cursor, incident_id, task_id, claim) -> bool:
    """Store the Celery id only if this claim is still the current one."""
    if not claim:
        return False
    cursor.execute(
        """UPDATE incidents SET rca_celery_task_id = %s
           WHERE id = %s AND rca_celery_task_id = %s""",
        (task_id, incident_id, claim),
    )
    return cursor.rowcount > 0


def try_reopen_incident(cursor, *, log_prefix: str, **kwargs) -> Optional[str]:
    """Reopen inside a savepoint. None means the row was not reset."""
    try:
        cursor.execute("SAVEPOINT sp_reopen_incident")
        claim = reopen_incident(cursor, **kwargs)
        cursor.execute("RELEASE SAVEPOINT sp_reopen_incident")
        return claim
    except Exception as exc:
        # Leave the incident as it was and let the next firing retry the reopen.
        try:
            cursor.execute("ROLLBACK TO SAVEPOINT sp_reopen_incident")
        except Exception as rb_exc:
            logger.debug("%s Rollback to sp_reopen_incident failed: %s", log_prefix, rb_exc)
        logger.warning("%s Failed to reopen incident %s: %s", log_prefix, kwargs.get("incident_id"), exc)
        return None
