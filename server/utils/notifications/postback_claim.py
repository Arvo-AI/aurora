"""
Claim-before-POST guard for the RCA notes Aurora posts back onto a paging
system (PagerDuty notes, incident.io incident updates).

A post-back is permanent on the other side: PagerDuty has no edit/delete
endpoint and an incident.io update notifies the responders the moment it
lands. So each post is guarded by a claim on a per-system column of the
incidents row: the column is set to 'pending' before the POST and to the
remote object id after it. The claim is released only when the remote side
definitively rejected the POST (4xx); when the outcome is unknown (timeout,
connection reset, 5xx a gateway may have returned after the note was stored)
it stays 'pending'. A claim that never resolves means a lost note, never a
duplicate.
"""

import logging

from utils.auth.stateless_auth import set_rls_context
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

PENDING = "pending"
# One statement set per post-back column (claim, release, record): kept as
# literals so no column name is ever interpolated into SQL.
_STATEMENTS = {
    "pagerduty_note_id": (
        "UPDATE incidents SET pagerduty_note_id = %s WHERE id = %s AND pagerduty_note_id IS NULL",
        "UPDATE incidents SET pagerduty_note_id = NULL WHERE id = %s AND pagerduty_note_id = %s",
        "UPDATE incidents SET pagerduty_note_id = %s WHERE id = %s",
    ),
    "incidentio_update_id": (
        "UPDATE incidents SET incidentio_update_id = %s WHERE id = %s AND incidentio_update_id IS NULL",
        "UPDATE incidents SET incidentio_update_id = NULL WHERE id = %s AND incidentio_update_id = %s",
        "UPDATE incidents SET incidentio_update_id = %s WHERE id = %s",
    ),
}


class PostbackClaim:
    """Claim / release / record on one incidents column."""

    def __init__(self, column: str, log_prefix: str):
        if column not in _STATEMENTS:
            raise ValueError(f"unknown post-back column {column!r}")
        self.column = column
        self._claim_sql, self._release_sql, self._record_sql = _STATEMENTS[column]
        self._log = log_prefix

    def _update(self, user_id: str, sql: str, params: tuple) -> int:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                if not set_rls_context(cursor, conn, user_id, log_prefix=self._log):
                    # Without RLS vars the UPDATE silently matches 0 rows
                    raise RuntimeError(f"cannot resolve org for user {user_id}")
                cursor.execute(sql, params)
                rowcount = cursor.rowcount
            conn.commit()
        return rowcount

    def claim(self, incident_id: str, user_id: str) -> bool:
        """Mark the incident 'pending' iff nothing is posted or in flight."""
        try:
            return self._update(
                user_id,
                self._claim_sql,
                (PENDING, incident_id),
            ) == 1
        except Exception:
            logger.exception("%s Failed to claim incident %s", self._log, incident_id)
            return False

    def release(self, incident_id: str, user_id: str) -> None:
        """Undo a claim whose POST was definitively rejected, so a later completion can retry."""
        try:
            self._update(
                user_id,
                self._release_sql,
                (incident_id, PENDING),
            )
        except Exception:
            logger.exception("%s Failed to release claim on incident %s", self._log, incident_id)

    def record(self, incident_id: str, user_id: str, remote_id: str) -> None:
        try:
            self._update(
                user_id,
                self._record_sql,
                (remote_id, incident_id),
            )
        except Exception:
            # The note is posted; the row stays 'pending', which still blocks a duplicate.
            logger.exception("%s Failed to record %s on incident %s", self._log, remote_id, incident_id)
