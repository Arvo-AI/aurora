"""
One-time backfill: replace the placeholder description on existing postmortem
memory entries ("Postmortem for incident: <title>") with the cause-bearing
description that new saves now get via ``_postmortem_description``.

Idempotent: only rows whose description still has the placeholder prefix are
considered, and rows whose recomputed description is unchanged (no Root Cause
section) are skipped, so a second run updates 0 rows.

Deliberately does NOT create an artifact version (versions track ``content``;
description is metadata, and the content is unchanged) and does NOT bump
``updated_at`` (that would reorder recency-based ranking for every org).
"""

import logging

from celery_config import celery_app
from utils.auth.stateless_auth import set_rls_context
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

OLD_PREFIX = "Postmortem for incident: "
BATCH_SIZE = 200


def _new_description(old: str, content: str) -> str | None:
    """Return the replacement description, or None when nothing should change."""
    from chat.backend.agent.tools.postmortem_tool import _postmortem_description

    title = old[len(OLD_PREFIX):].strip()
    if not title:
        return None
    new = _postmortem_description(content, title, None)
    if new == old or new.startswith(OLD_PREFIX):
        return None
    return new


def _backfill_org(cursor, conn, org_id: str, user_id: str) -> int:
    """Backfill one org (RLS context must already be set). Returns rows updated."""
    updated = 0
    last_id = None
    while True:
        cursor.execute(
            """SELECT id, description, content FROM artifacts
               WHERE category = 'postmortem'
                 AND description LIKE 'Postmortem for incident: %%'
                 AND (%s::uuid IS NULL OR id > %s::uuid)
               ORDER BY id LIMIT %s""",
            (last_id, last_id, BATCH_SIZE),
        )
        rows = cursor.fetchall()
        if not rows:
            break
        for art_id, old, content in rows:
            last_id = str(art_id)
            new = _new_description(old, content)
            if new is None:
                continue
            # Match on the old description too: never clobber a concurrent edit.
            cursor.execute(
                """UPDATE artifacts SET description = %s
                   WHERE id = %s AND category = 'postmortem' AND description = %s""",
                (new, art_id, old),
            )
            updated += cursor.rowcount or 0
        if len(rows) < BATCH_SIZE:
            break
    return updated


@celery_app.task(name="backfill_postmortem_descriptions", bind=True, max_retries=3, default_retry_delay=60)
def backfill_postmortem_descriptions(self):
    """Backfill placeholder postmortem descriptions across all orgs."""
    try:
        return run_backfill()
    except Exception as exc:
        logger.exception("[PostmortemBackfill] Fatal error")
        raise self.retry(exc=exc)


def run_backfill() -> dict:
    """Plain-function body of the task (importable/testable without Celery)."""
    stats = {"orgs": 0, "updated": 0, "errors": 0}
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            # users is not RLS-protected: one representative user per org.
            cursor.execute(
                "SELECT org_id, MIN(id) FROM users WHERE org_id IS NOT NULL GROUP BY org_id"
            )
            orgs = cursor.fetchall()

            for org_id, user_id in orgs:
                try:
                    if not set_rls_context(
                        cursor, conn, user_id, org_id=org_id,
                        log_prefix="[PostmortemBackfill]",
                    ):
                        stats["errors"] += 1
                        continue
                    n = _backfill_org(cursor, conn, org_id, user_id)
                    conn.commit()
                    stats["orgs"] += 1
                    stats["updated"] += n
                    if n:
                        logger.info("[PostmortemBackfill] org %s: updated %d", org_id, n)
                except Exception as e:
                    # Row/org-level failure: roll back this org only, keep going.
                    logger.warning("[PostmortemBackfill] org %s failed: %s", org_id, type(e).__name__)
                    stats["errors"] += 1
                    try:
                        conn.rollback()
                    except Exception:
                        pass
    logger.info("[PostmortemBackfill] Complete: %s", stats)
    return stats
