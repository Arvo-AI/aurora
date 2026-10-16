"""Keep scheduled actions owned by a living admin.

Scheduled runs execute as ``actions.created_by``. That column has no foreign
key, so deleting the creator makes every later run fail at session creation.
"""
import logging
from typing import Optional

from utils.auth import ROLE_ADMIN

logger = logging.getLogger(__name__)

# departing_user_id is NULL when healing rows whose creator is already gone.
# ``= NULL`` matches nothing, so only the NOT EXISTS branch applies.
_REASSIGN_SQL = """
    UPDATE actions a
    SET created_by = admin.id, updated_at = NOW()
    FROM (
        SELECT id FROM users
        WHERE org_id = %s AND role = %s AND id IS DISTINCT FROM %s
        ORDER BY created_at
        LIMIT 1
    ) admin
    WHERE a.org_id = %s
      AND (
          a.created_by = %s
          OR NOT EXISTS (
              SELECT 1 FROM users u
              WHERE u.id = a.created_by AND u.org_id = a.org_id
          )
      )
    RETURNING a.id
"""

_LEFTOVER_SQL = """
    SELECT COUNT(*) FROM actions a
    WHERE a.org_id = %s
      AND (
          a.created_by = %s
          OR NOT EXISTS (
              SELECT 1 FROM users u
              WHERE u.id = a.created_by AND u.org_id = a.org_id
          )
      )
"""


def reassign_actions_to_admin(
    cursor, org_id: str, departing_user_id: Optional[str] = None
) -> int:
    """Point this user's actions, and any already-orphaned ones, at another admin.

    Returns how many rows moved. Zero when the org has no other admin; the
    caller still deletes the user, and a later scheduler pass retries.
    """
    if not org_id:
        return 0

    cursor.execute(
        _REASSIGN_SQL,
        (org_id, ROLE_ADMIN, departing_user_id, org_id, departing_user_id),
    )
    moved = cursor.fetchall()
    # Another admin exists — the actions now run as that account.
    if moved:
        logger.info(
            "[Actions] Reassigned %d action(s) to another admin",
            len(moved),
        )
        return len(moved)

    cursor.execute(_LEFTOVER_SQL, (org_id, departing_user_id))
    leftover = cursor.fetchone()[0]
    # The hourly-or-minute heal would repeat this for an org with no other admin.
    if leftover and departing_user_id:
        logger.warning(
            "[Actions] %d action(s) still have no living owner; no other admin to assign",
            leftover,
        )
    return 0
