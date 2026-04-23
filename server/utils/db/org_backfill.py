"""
Centralized org_id backfill for all user-scoped data.

This is the SINGLE SOURCE OF TRUTH for stamping org_id across the database.
It handles two categories of tables:

  1. User-scoped tables  — have (user_id, org_id).  Discovered dynamically via
     information_schema so new tables are automatically covered.
  2. Incident child tables — have (incident_id, org_id) but no user_id.
     Org is inherited from the parent incident row.

Four entry points call into this module:
  • /setup-org   — user creates their first org      → backfill_user_org_data()
  • add-member   — user is added to an existing org  → backfill_user_org_data()
  • org transfer — user moves to a different org     → migrate_user_to_org()
  • server boot  — catch-up for any rows still NULL  → backfill_all_users_at_boot()

No other file should contain org_id backfill logic.
"""

import logging

from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────────────
# Schema introspection queries (cached per-call, not per-import)
# ────────────────────────────────────────────────────────────────────────────

_USER_SCOPED_TABLES_SQL = """
    SELECT c1.table_name
    FROM information_schema.columns c1
    JOIN information_schema.columns c2
      ON c1.table_name = c2.table_name
     AND c1.table_schema = c2.table_schema
    JOIN information_schema.tables t
      ON t.table_name = c1.table_name
     AND t.table_schema = c1.table_schema
     AND t.table_type = 'BASE TABLE'
    WHERE c1.column_name = 'user_id'
      AND c2.column_name = 'org_id'
      AND c1.table_schema = 'public'
      AND c1.table_name NOT IN ('users', 'user_preferences')
    ORDER BY c1.table_name;
"""

_INCIDENT_CHILD_TABLES_SQL = """
    SELECT c1.table_name
    FROM information_schema.columns c1
    JOIN information_schema.columns c2
      ON c1.table_name = c2.table_name
     AND c1.table_schema = c2.table_schema
    JOIN information_schema.tables t
      ON t.table_name = c1.table_name
     AND t.table_schema = c1.table_schema
     AND t.table_type = 'BASE TABLE'
    WHERE c1.column_name = 'incident_id'
      AND c2.column_name = 'org_id'
      AND c1.table_schema = 'public'
      AND c1.table_name NOT IN (
          SELECT table_name FROM information_schema.columns
          WHERE column_name = 'user_id' AND table_schema = 'public'
      )
    ORDER BY c1.table_name;
"""


def _safe_update(cursor, label: str, sql: str, params: tuple) -> int:
    """Execute an UPDATE inside a SAVEPOINT so one failure can't abort the tx."""
    sp = f"sp_{label}"
    try:
        cursor.execute(f"SAVEPOINT {sp}")
        cursor.execute(sql, params)
        updated = cursor.rowcount
        cursor.execute(f"RELEASE SAVEPOINT {sp}")
        return updated
    except Exception as e:
        logger.warning("Backfill failed on %s: %s", label, e)
        try:
            cursor.execute(f"ROLLBACK TO SAVEPOINT {sp}")
        except Exception:
            logger.debug("ROLLBACK TO SAVEPOINT also failed for %s", label)
        return 0


# ────────────────────────────────────────────────────────────────────────────
# Single-user backfill  (called from /setup-org and add-member)
# ────────────────────────────────────────────────────────────────────────────

def backfill_user_org_data(cursor, user_id: str, org_id: str) -> dict:
    """Stamp org_id on every row belonging to a single user.

    Called when a user creates or joins an org.  Covers:
      • All tables with (user_id, org_id) — dynamic discovery
      • Incident child tables with (incident_id, org_id) — via parent FK

    Re-stamps rows that already have a stale org_id (e.g. from a defunct
    Default Organization) in addition to NULL rows, so that pre-RBAC users
    who never had users.org_id set still get all their data migrated.

    Args:
        cursor: An open psycopg2 cursor (caller owns the transaction).
        user_id: The user whose data should be stamped.
        org_id:  The org_id to write.

    Returns:
        dict  {table_name: rows_updated}
    """
    results = {}

    # Phase 1 — user-scoped tables
    cursor.execute(_USER_SCOPED_TABLES_SQL)
    for (tbl,) in cursor.fetchall():
        n = _safe_update(
            cursor, f"user_{tbl}",
            f'UPDATE "{tbl}" SET org_id = %s WHERE user_id = %s AND (org_id IS NULL OR org_id != %s)',
            (org_id, user_id, org_id),
        )
        if n:
            results[tbl] = n

    # Phase 2 — incident child tables (no user_id; inherit from parent incident)
    cursor.execute(_INCIDENT_CHILD_TABLES_SQL)
    for (tbl,) in cursor.fetchall():
        n = _safe_update(
            cursor, f"child_{tbl}",
            f'UPDATE "{tbl}" c SET org_id = %s '
            f"FROM incidents i WHERE c.incident_id = i.id "
            f"AND i.user_id = %s AND (c.org_id IS NULL OR c.org_id != %s)",
            (org_id, user_id, org_id),
        )
        if n:
            results[tbl] = n

    if results:
        total = sum(results.values())
        logger.info(
            "Backfilled org_id for user %s across %d table(s): %d row(s) total — %s",
            user_id, len(results), total, results,
        )
    else:
        logger.info("No orphaned data to backfill for user %s", user_id)

    return results


# ────────────────────────────────────────────────────────────────────────────
# Cross-org migration  (called when a user transfers between organizations)
# ────────────────────────────────────────────────────────────────────────────

def migrate_user_to_org(cursor, user_id: str, new_org_id: str) -> dict:
    """Move ALL of a user's data from their current org to a new org.

    Similar to backfill_user_org_data but used specifically when a user
    transfers between organizations with a known old org (e.g. joining a
    team's org after creating their own).

    Args:
        cursor: An open psycopg2 cursor (caller owns the transaction).
        user_id: The user whose data should be moved.
        new_org_id: The destination org_id.

    Returns:
        dict {table_name: rows_updated}
    """
    results = {}

    # Phase 1 — user-scoped tables (move ALL rows, not just NULL org_id)
    cursor.execute(_USER_SCOPED_TABLES_SQL)
    for (tbl,) in cursor.fetchall():
        n = _safe_update(
            cursor, f"migrate_{tbl}",
            f'UPDATE "{tbl}" SET org_id = %s WHERE user_id = %s',
            (new_org_id, user_id),
        )
        if n:
            results[tbl] = n

    # Phase 2 — incident child tables (no user_id; inherit from parent incident)
    cursor.execute(_INCIDENT_CHILD_TABLES_SQL)
    for (tbl,) in cursor.fetchall():
        n = _safe_update(
            cursor, f"migrate_child_{tbl}",
            f'UPDATE "{tbl}" c SET org_id = %s '
            f"FROM incidents i WHERE c.incident_id = i.id "
            f"AND i.user_id = %s",
            (new_org_id, user_id),
        )
        if n:
            results[tbl] = n

    if results:
        total = sum(results.values())
        logger.info(
            "Migrated user %s to org %s across %d table(s): %d row(s) total — %s",
            sanitize(user_id), sanitize(new_org_id), len(results), total, results,
        )
    else:
        logger.info("No data to migrate for user %s to org %s", sanitize(user_id), sanitize(new_org_id))

    return results


# ────────────────────────────────────────────────────────────────────────────
# Bulk backfill  (called once at server boot from db_utils.initialize_tables)
# ────────────────────────────────────────────────────────────────────────────

def backfill_all_users_at_boot(cursor) -> None:
    """Catch-up backfill for every user who already has an org.

    Uses a JOIN against the users table so it stamps all users in one UPDATE
    per table (much faster than looping per-user for large deployments).
    """
    cursor.execute("SELECT COUNT(*) FROM users WHERE org_id IS NOT NULL")
    if cursor.fetchone()[0] == 0:
        return

    results = {}

    # Phase 1 — user-scoped tables (bulk JOIN)
    # Fix NULL org_id AND stale org_id that doesn't match the user's current org
    cursor.execute(_USER_SCOPED_TABLES_SQL)
    for (tbl,) in cursor.fetchall():
        n = _safe_update(
            cursor, f"boot_{tbl}",
            f'UPDATE "{tbl}" t SET org_id = u.org_id '
            f"FROM users u WHERE t.user_id = u.id "
            f"AND u.org_id IS NOT NULL "
            f"AND (t.org_id IS NULL OR t.org_id != u.org_id)",
            (),
        )
        if n:
            results[tbl] = n
            logger.info("[DBG] boot backfill: stamped %d rows in %s", n, tbl)

    # Phase 2 — incident child tables (bulk JOIN via incident)
    cursor.execute(_INCIDENT_CHILD_TABLES_SQL)
    for (tbl,) in cursor.fetchall():
        n = _safe_update(
            cursor, f"boot_child_{tbl}",
            f'UPDATE "{tbl}" c SET org_id = i.org_id '
            f"FROM incidents i WHERE c.incident_id = i.id "
            f"AND i.org_id IS NOT NULL "
            f"AND (c.org_id IS NULL OR c.org_id != i.org_id)",
            (),
        )
        if n:
            results[tbl] = n

    if results:
        total = sum(results.values())
        logger.info(
            "Boot backfill: stamped org_id on %d table(s), %d row(s) — %s",
            len(results), total, results,
        )

    cursor.execute("SELECT COUNT(*) FROM users WHERE org_id IS NULL")
    orphans = cursor.fetchone()[0]
    if orphans:
        logger.info(
            "%d user(s) without an org — they will be prompted to "
            "create one at next login via /setup-org.",
            orphans,
        )
