"""Helpers for reading incident state from the DB."""

import logging
from datetime import timezone
from typing import Optional

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
