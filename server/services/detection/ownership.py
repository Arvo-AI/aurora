"""Keep checks owned by someone who is still in the org.

A check runs with the connections of ``detection_checks.created_by``. That
column has no foreign key, so when its creator leaves the org every later run
errors.

Scheduled actions hand an orphan to the oldest admin and carry on
(services/actions/ownership.py). A check cannot quite do that: its command and
target were written by the person who left, and under an admin's connections
the same command may reach an account its author never could. So the hand-off
here stops short. The admin becomes the owner, the check is paused, and it runs
again only when an editor looks at it and resumes it. Whoever resumes it owns
it from then on, and it runs with their access.
"""
import logging
from typing import List, Optional, Tuple

from services.detection import STATE_RETIRED
from utils.auth import ROLE_ADMIN

logger = logging.getLogger(__name__)

PAUSED_OWNER_LEFT = "Its owner left the organization. It will run with your access once you resume it."

_REASSIGN_SQL = """
    UPDATE detection_checks c
    SET created_by = admin.id, paused_at = NOW(), paused_reason = %s,
        claimed_at = NULL, updated_at = NOW()
    FROM (
        SELECT id FROM users
        WHERE org_id = %s AND role = %s
        ORDER BY created_at
        LIMIT 1
    ) admin
    WHERE c.org_id = %s AND c.state <> %s
      AND NOT EXISTS (
          SELECT 1 FROM users u
          WHERE u.id = c.created_by AND u.org_id = c.org_id
      )
    RETURNING c.name, admin.id
"""


def reassign_orphaned_checks(cursor, org_id: str) -> Tuple[Optional[str], List[str]]:
    """Hand checks whose creator has left to an admin, paused.

    Returns (the admin's user id, the names handed over), or (None, []) when
    nothing moved. Nothing moves when the org has no admin; the next pass
    tries again.
    """
    if not org_id:
        return None, []
    cursor.execute(_REASSIGN_SQL, (PAUSED_OWNER_LEFT, org_id, ROLE_ADMIN, org_id, STATE_RETIRED))
    rows = cursor.fetchall()
    if not rows:
        return None, []
    logger.info("[DetectionOwnership] Handed %d check(s) to an admin, paused", len(rows))
    return rows[0][1], [row[0] for row in rows]


def resume_check(cursor, org_id: str, check_id: str, user_id: str) -> bool:
    """An editor has looked at a paused check and wants it running, as themselves."""
    cursor.execute(
        """UPDATE detection_checks
           SET paused_at = NULL, paused_reason = NULL, created_by = %s, last_run_at = NULL,
               updated_at = NOW()
           WHERE id = %s AND org_id = %s AND paused_at IS NOT NULL AND state <> %s""",
        (user_id, check_id, org_id, STATE_RETIRED),
    )
    return cursor.rowcount > 0


def handed_over_message(names: List[str]) -> str:
    """What the org's Slack installer is told. Plain text, nothing to escape but names."""
    from services.detection.attention import slack_text

    shown = ", ".join("`%s`" % slack_text(name) for name in names[:10])
    more = " and %d more" % (len(names) - 10) if len(names) > 10 else ""
    return (
        "%d of my checks belonged to someone who is no longer in your organization: %s%s. "
        "I have paused them instead of running them with someone else's access. "
        "An editor can look at each one on the Checks page and resume or stop it."
        % (len(names), shown, more)
    )
