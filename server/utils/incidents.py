"""Helpers for reading incident state from the DB."""

import logging
from datetime import datetime, timezone
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


def fetch_incident_start_time(user_id: str, incident_id: Optional[str], log_prefix: str = "[incidents]") -> Optional[str]:
    """Return the incident's ``started_at`` as an ISO 8601 UTC string, or None.

    Sets RLS context with ``user_id`` before reading the RLS-protected
    ``incidents`` table. Returns None on missing incident, missing
    ``started_at``, or any error (logged at warning level).
    """
    if not incident_id:
        return None
    try:
        from utils.db.connection_pool import db_pool
        from utils.auth.stateless_auth import set_rls_context
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix=log_prefix)
                cur.execute(
                    "SELECT started_at FROM incidents WHERE id = %s",
                    (incident_id,),
                )
                row = cur.fetchone()
                if not row or not row[0]:
                    return None
                started_at = row[0]
                if started_at.tzinfo is None:
                    started_at = started_at.replace(tzinfo=timezone.utc)
                return started_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception as exc:
        logger.warning("%s Could not fetch incident started_at for %s: %s", log_prefix, incident_id, exc)
        return None


def parse_incident_time(raw) -> Tuple[Optional[datetime], Optional[str]]:
    """Parse an ISO 8601 incident time into naive UTC (the column type).

    Returns (value, None) on success, (None, None) for empty input,
    and (None, error_message) when invalid or in the future.
    """
    raw = str(raw or "").strip()
    if not raw:
        return None, None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None, "incident_time must be ISO 8601"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if parsed > datetime.now(timezone.utc):
        return None, "incident_time cannot be in the future"
    return parsed.astimezone(timezone.utc).replace(tzinfo=None), None


def fetch_incident_public_repo(user_id: str, incident_id: Optional[str], log_prefix: str = "[incidents]") -> Optional[str]:
    """Return the public 'owner/repo' a sandbox RCA was started with, or None."""
    if not incident_id:
        return None
    try:
        from utils.db.connection_pool import db_pool
        from utils.auth.stateless_auth import set_rls_context
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix=log_prefix)
            cur.execute(
                "SELECT alert_metadata->>'public_repo' FROM incidents WHERE id = %s",
                (incident_id,),
            )
            row = cur.fetchone()
            return row[0] if row and row[0] else None
    except Exception as exc:
        logger.warning("%s Could not fetch public_repo for %s: %s", log_prefix, incident_id, exc)
        return None
