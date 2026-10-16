"""Keep org connectors owned by a living admin.

Connectors are org-shared, but ``user_tokens`` and ``user_connections`` are
keyed by the user who connected them. Deleting that user used to delete the
rows, which dropped the credential for everyone still in the org.
"""
import logging
from typing import Optional

from utils.auth import ROLE_ADMIN

logger = logging.getLogger(__name__)

# A grant issued to the departing person. Moving the row would keep their
# refresh token working as the admin, so these are deleted instead.
PERSONAL_OAUTH_PROVIDERS = (
    "bitbucket",
    "bitbucket_workspace_selection",
    "confluence",
    "gcp",
    "jira",
    "notion",
    "sharepoint",
    "slack",
    "teams",
)

# RLS hides org_id IS NULL, so a null-org row is never visible here.
_ADMIN_SQL = """
    SELECT id FROM users
    WHERE org_id = %s AND role = %s AND id IS DISTINCT FROM %s
    ORDER BY created_at
    LIMIT 1
"""

_OWNED_SQL = """
    {a}.org_id = %s
    AND (
        {a}.user_id = %s
        OR NOT EXISTS (
            SELECT 1 FROM users u
            WHERE u.id = {a}.user_id AND u.org_id = {a}.org_id
        )
    )
"""

# user_connections is unique on (user_id, provider, account_id).
_CONN_FREE_SQL = """
    NOT EXISTS (
        SELECT 1 FROM user_connections other
        WHERE other.id <> c.id
          AND other.user_id = admin.id
          AND other.provider = c.provider
          AND other.account_id = c.account_id
    )
"""

_REASSIGN_TOKENS_SQL = f"""
    UPDATE user_tokens t
    SET user_id = admin.id
    FROM ({_ADMIN_SQL}) admin
    WHERE {_OWNED_SQL.format(a="t")}
      AND NOT (t.provider = ANY(%s))
    RETURNING t.id
"""

_DELETE_PERSONAL_SQL = f"""
    DELETE FROM user_tokens t
    WHERE {_OWNED_SQL.format(a="t")}
      AND t.provider = ANY(%s)
"""

_REASSIGN_CONNS_SQL = f"""
    UPDATE user_connections c
    SET user_id = admin.id
    FROM ({_ADMIN_SQL}) admin
    WHERE {_OWNED_SQL.format(a="c")}
      AND {_CONN_FREE_SQL}
    RETURNING c.id
"""

_DROP_CONNS_SQL = f"""
    DELETE FROM user_connections c
    USING ({_ADMIN_SQL}) admin
    WHERE {_OWNED_SQL.format(a="c")}
      AND NOT ({_CONN_FREE_SQL})
"""

_LEFTOVER_SQL = f"""
    SELECT
        (SELECT COUNT(*) FROM user_tokens t
         WHERE {_OWNED_SQL.format(a="t")}
           AND NOT (t.provider = ANY(%s)))
      + (SELECT COUNT(*) FROM user_connections c
         WHERE {_OWNED_SQL.format(a="c")})
"""


def _owned(org_id: str, departing_user_id: Optional[str]) -> tuple:
    return (org_id, departing_user_id)


def _admin(org_id: str, departing_user_id: Optional[str]) -> tuple:
    return (org_id, ROLE_ADMIN, departing_user_id, *_owned(org_id, departing_user_id))


def _changed(cursor) -> int:
    return cursor.rowcount if cursor.rowcount > 0 else 0


def reassign_connectors_to_admin(
    cursor, org_id: str, departing_user_id: Optional[str] = None
) -> int:
    """Point org credentials at another admin. Delete personal OAuth instead.

    Returns how many rows moved or deleted. The caller commits only when this
    is nonzero. Zero when nothing changed.
    """
    if not org_id:
        return 0

    personal = list(PERSONAL_OAUTH_PROVIDERS)
    owned = _owned(org_id, departing_user_id)
    admin = _admin(org_id, departing_user_id)

    cursor.execute(_REASSIGN_TOKENS_SQL, (*admin, personal))
    moved = len(cursor.fetchall())
    cursor.execute(_REASSIGN_CONNS_SQL, admin)
    moved += len(cursor.fetchall())
    # The admin already has this account. Drop the departing copy.
    cursor.execute(_DROP_CONNS_SQL, admin)
    dropped = _changed(cursor)
    # Personal grants are not transferred.
    cursor.execute(_DELETE_PERSONAL_SQL, (*owned, personal))
    dropped += _changed(cursor)

    if dropped:
        logger.info("[Connectors] Dropped %d connector row(s)", dropped)
    if moved:
        logger.info(
            "[Connectors] Reassigned %d connector row(s) to another admin",
            moved,
        )
    if moved or dropped:
        return moved + dropped

    # Only the removal path warns. The hourly heal would repeat it forever.
    if not departing_user_id:
        return 0

    cursor.execute(_LEFTOVER_SQL, (*owned, personal, *owned))
    leftover = cursor.fetchone()[0]
    if leftover:
        logger.warning(
            "[Connectors] %d connector row(s) still have no living owner; no other admin to assign",
            leftover,
        )
    return 0
