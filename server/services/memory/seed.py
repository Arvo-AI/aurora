"""Create a well-known memory entry with default content, never overwriting one."""

import logging

from utils.db.connection_pool import db_pool
from utils.auth.stateless_auth import set_rls_context
from utils.log_sanitizer import sanitize
from services.artifacts.store import create_version

logger = logging.getLogger(__name__)


def seed_memory_entry(
    user_id: str,
    *,
    category: str,
    title: str,
    content: str,
    description: str,
    org_id: str | None = None,
    log_prefix: str = "[MemorySeed]",
) -> bool:
    """Insert the entry if absent. Returns True only when a new row was created.

    Pass ``org_id`` from request handlers so the seed lands in the org the caller
    reads back; without it ``users.org_id`` is used, which is TTL-cached.
    """
    if not user_id:
        return False

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                # The INSERT must run under the same org or FORCE ROW LEVEL SECURITY rejects it.
                org_id = set_rls_context(
                    cursor, conn, user_id, org_id=org_id, log_prefix=f"{log_prefix}:seed"
                )
                if not org_id:
                    logger.warning("%s No org for user; cannot seed memory", log_prefix)
                    return False

                # Already present — never overwrite user/agent edits.
                cursor.execute(
                    """SELECT id FROM artifacts
                       WHERE org_id = %s AND category = %s AND title = %s""",
                    (org_id, category, title),
                )
                if cursor.fetchone():
                    return False

                # last_edited_by='agent' since Aurora created it; a UI edit flips it to 'user'.
                cursor.execute(
                    """INSERT INTO artifacts
                           (org_id, user_id, title, content, category, description,
                            last_edited_by, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s, 'agent', CURRENT_TIMESTAMP)
                       ON CONFLICT (org_id, category, title) DO NOTHING
                       RETURNING id""",
                    (org_id, user_id, title, content, category, description),
                )
                row = cursor.fetchone()
                # A concurrent seed won the race (ON CONFLICT DO NOTHING) — fine.
                if not row:
                    conn.commit()
                    return False

                create_version(cursor, str(row[0]), org_id, user_id, content, source="agent")
                conn.commit()
                # org_id can come from a request header, so strip control chars first.
                logger.info("%s Seeded default memory for org %s", log_prefix, sanitize(org_id))
                return True
    except Exception:
        logger.exception("%s Failed to seed memory", log_prefix)
        return False
