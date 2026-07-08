"""
Memory data-access helpers.

Shared DB queries for fetching memory entries (artifacts table).
Used by both the index builder and the injector.
"""

import logging
from typing import Dict, List

from utils.db.connection_pool import db_pool
from utils.auth.stateless_auth import set_rls_context

from services.memory import MEMORY_CATEGORIES

logger = logging.getLogger(__name__)


def get_memory_entries(user_id: str, limit: int = 200) -> List[Dict]:
    """Fetch memory entry metadata (no content) for the user's org.

    Returns a list of dicts with: category, title, description, updated_at.
    Ordered by most recently updated first.
    """
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                org_id = set_rls_context(cursor, conn, user_id, log_prefix="[MemoryIndex]")
                if not org_id:
                    return []

                cursor.execute(
                    """SELECT category, title, description, updated_at
                       FROM artifacts
                       WHERE org_id = %s AND category = ANY(%s)
                       ORDER BY updated_at DESC
                       LIMIT %s""",
                    (org_id, list(MEMORY_CATEGORIES), limit),
                )
                return [
                    {"category": r[0], "title": r[1], "description": r[2] or "", "updated_at": r[3]}
                    for r in cursor.fetchall()
                ]
    except Exception as e:
        logger.warning("[MemoryQueries] Failed to fetch entries for user %s: %s", user_id, e)
        return []


def fetch_memory_content(user_id: str, entries: List[Dict]) -> List[Dict]:
    """Fetch full content for a list of memory entries.

    Takes entries (with category + title) and returns them enriched with content.
    """
    if not entries:
        return []

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                org_id = set_rls_context(cursor, conn, user_id, log_prefix="[MemoryQueries:fetch]")
                if not org_id:
                    return []

                results = []
                for entry in entries:
                    cursor.execute(
                        """SELECT content, updated_at FROM artifacts
                           WHERE org_id = %s AND category = %s AND title = %s""",
                        (org_id, entry["category"], entry["title"]),
                    )
                    row = cursor.fetchone()
                    if row:
                        results.append({
                            "category": entry["category"],
                            "title": entry["title"],
                            "content": row[0] or "",
                            "updated_at": row[1],
                        })
                return results
    except Exception:
        logger.exception("[MemoryQueries] Failed to fetch memory content")
        return []
