"""Keep checks owned by someone who is still in the org.

A check runs with the connections of ``detection_checks.created_by``. That
column has no foreign key, so when its creator leaves the org every later run
errors. Same hand-off as scheduled actions (services/actions/ownership.py):
the oldest admin takes over.
"""
import logging

from utils.auth import ROLE_ADMIN

logger = logging.getLogger(__name__)

_REASSIGN_SQL = """
    UPDATE detection_checks c
    SET created_by = admin.id, updated_at = NOW()
    FROM (
        SELECT id FROM users
        WHERE org_id = %s AND role = %s
        ORDER BY created_at
        LIMIT 1
    ) admin
    WHERE c.org_id = %s
      AND NOT EXISTS (
          SELECT 1 FROM users u
          WHERE u.id = c.created_by AND u.org_id = c.org_id
      )
    RETURNING c.id
"""


def reassign_orphaned_checks(cursor, org_id: str) -> int:
    """Hand checks whose creator has left to an admin. Returns how many moved.

    Zero when the org has no admin; the next pass tries again.
    """
    if not org_id:
        return 0
    cursor.execute(_REASSIGN_SQL, (org_id, ROLE_ADMIN, org_id))
    moved = cursor.rowcount
    if moved:
        logger.info("[DetectionOwnership] Reassigned %d check(s) to an admin", moved)
    return moved
