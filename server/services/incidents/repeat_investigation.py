"""Whether a repeat of an existing incident needs another RCA.

Metric samples and templated annotation text are not part of the identity.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple, Optional

logger = logging.getLogger(__name__)

# Written into rca_celery_task_id while a request is enqueueing, so a second
# delivery of the same alert cannot start a parallel investigation. Replaced
# with the real Celery id once delay() returns.
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
    updated_at: Any


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


def repeat_needs_investigation(
    previous_status,
    previous_signature,
    new_signature,
    *,
    session_id=None,
    task_id=None,
    updated_at=None,
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
    if task_id == RCA_ENQUEUE_CLAIM:
        if updated_at is None:
            return False
        current = now or datetime.now(timezone.utc)
        claimed_at = updated_at
        if claimed_at.tzinfo is None:
            claimed_at = claimed_at.replace(tzinfo=timezone.utc)
        return current - claimed_at > _CLAIM_TTL
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
        updated_at=existing.updated_at,
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
               aurora_chat_session_id, rca_celery_task_id, updated_at
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


def reopen_incident(cursor, *, incident_id, user_id, org_id, severity, title, service, started_at, previous_status, record_lifecycle=True) -> None:
    """Hand the incident's chat to a new RCA and claim the enqueue.

    The caller replaces rca_celery_task_id with the Celery id after delay().
    """
    cursor.execute(
        """
        UPDATE incidents
           SET status = 'investigating',
               analyzed_at = NULL,
               aurora_status = 'idle',
               rca_celery_task_id = %s,
               severity = %s,
               alert_title = %s,
               alert_service = %s,
               started_at = %s,
               updated_at = CURRENT_TIMESTAMP
         WHERE id = %s
        """,
        (RCA_ENQUEUE_CLAIM, severity, title, service, started_at, incident_id),
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


def try_reopen_incident(cursor, *, log_prefix: str, **kwargs) -> bool:
    """Reopen inside a savepoint. False means the row was not reset."""
    try:
        cursor.execute("SAVEPOINT sp_reopen_incident")
        reopen_incident(cursor, **kwargs)
        cursor.execute("RELEASE SAVEPOINT sp_reopen_incident")
        return True
    except Exception as exc:
        # Leave the incident as it was and let the next firing retry the reopen.
        try:
            cursor.execute("ROLLBACK TO SAVEPOINT sp_reopen_incident")
        except Exception as rb_exc:
            logger.debug("%s Rollback to sp_reopen_incident failed: %s", log_prefix, rb_exc)
        logger.warning("%s Failed to reopen incident %s: %s", log_prefix, kwargs.get("incident_id"), exc)
        return False
