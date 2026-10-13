"""Keep org connectors owned by a living admin.

Connectors are org-shared, but ``user_tokens`` and ``user_connections`` are
keyed by the user who connected them. Deleting that user used to delete the
rows, which dropped the credential for everyone still in the org.
"""
import logging
from typing import Optional

from utils.auth import ROLE_ADMIN

logger = logging.getLogger(__name__)

# departing_user_id is NULL when healing rows whose owner is already gone.
# ``= NULL`` matches nothing, so only the NOT EXISTS branch applies.
_ADMIN_SQL = """
    SELECT id, org_id FROM users
    WHERE org_id = %s AND role = %s AND id IS DISTINCT FROM %s
    ORDER BY created_at
    LIMIT 1
"""

_OWNED_SQL = """
    (
        {a}.org_id = %s
        OR ({a}.org_id IS NULL AND {a}.user_id = %s)
    )
    AND (
        {a}.user_id = %s
        OR NOT EXISTS (
            SELECT 1 FROM users u
            WHERE u.id = {a}.user_id AND u.org_id = {a}.org_id
        )
    )
"""

# A second row for the same org credential would violate the unique key.
_TOKEN_FREE_SQL = """
    NOT EXISTS (
        SELECT 1 FROM user_tokens other
        WHERE other.id <> t.id
          AND other.provider = t.provider
          AND other.org_id IS NOT DISTINCT FROM COALESCE(t.org_id, admin.org_id)
    )
"""

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
    SET user_id = admin.id,
        org_id = COALESCE(t.org_id, admin.org_id)
    FROM ({_ADMIN_SQL}) admin
    WHERE {_OWNED_SQL.format(a="t")}
      AND {_TOKEN_FREE_SQL}
    RETURNING t.id
"""

_REASSIGN_CONNS_SQL = f"""
    UPDATE user_connections c
    SET user_id = admin.id,
        org_id = COALESCE(c.org_id, admin.org_id)
    FROM ({_ADMIN_SQL}) admin
    WHERE {_OWNED_SQL.format(a="c")}
      AND {_CONN_FREE_SQL}
    RETURNING c.id
"""

# The copy that lost the unique-key race. The admin already has this credential.
_DROP_TOKENS_SQL = f"""
    DELETE FROM user_tokens t
    USING ({_ADMIN_SQL}) admin
    WHERE {_OWNED_SQL.format(a="t")}
      AND NOT ({_TOKEN_FREE_SQL})
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
         WHERE {_OWNED_SQL.format(a="t")})
      + (SELECT COUNT(*) FROM user_connections c
         WHERE {_OWNED_SQL.format(a="c")})
"""


def _params(org_id: str, departing_user_id: Optional[str]) -> tuple:
    # Admin pick, then the ownership predicate. Repeated per statement.
    return (
        org_id, ROLE_ADMIN, departing_user_id,
        org_id, departing_user_id, departing_user_id,
    )


def reassign_connectors_to_admin(
    cursor, org_id: str, departing_user_id: Optional[str] = None
) -> int:
    """Point this user's connectors, and any already-orphaned ones, at another admin.

    Returns how many rows moved or dropped. The scheduler commits only when
    this is nonzero. Zero when the org has no other admin; a later pass retries.
    """
    if not org_id:
        return 0

    params = _params(org_id, departing_user_id)
    cursor.execute(_REASSIGN_TOKENS_SQL, params)
    tokens = cursor.fetchall()
    cursor.execute(_REASSIGN_CONNS_SQL, params)
    conns = cursor.fetchall()
    # Duplicates of a credential the admin already owns.
    cursor.execute(_DROP_TOKENS_SQL, params)
    dropped = cursor.rowcount if cursor.rowcount > 0 else 0
    cursor.execute(_DROP_CONNS_SQL, params)
    dropped += cursor.rowcount if cursor.rowcount > 0 else 0

    moved = len(tokens) + len(conns)
    # Another admin exists — the connectors now resolve as that account.
    if dropped:
        logger.info("[Connectors] Dropped %d duplicate connector row(s)", dropped)
    if moved:
        logger.info(
            "[Connectors] Reassigned %d connector row(s) to another admin",
            moved,
        )
    # Include drops so a tick that only deletes duplicates still commits.
    if moved or dropped:
        return moved + dropped

    # Ownership predicate is applied twice, once per table.
    cursor.execute(_LEFTOVER_SQL, params[3:] + params[3:])
    leftover = cursor.fetchone()[0]
    # Nothing to move, or the only admin is the one leaving.
    if leftover:
        logger.warning(
            "[Connectors] %d connector row(s) still have no living owner; no other admin to assign",
            leftover,
        )
    return 0
