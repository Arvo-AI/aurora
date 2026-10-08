"""
Memory data-access helpers.

Shared DB queries for fetching memory entries (artifacts table).
Used by the index builder, the injector, and the memory API routes.

Two flavours live here, and the difference matters:

* ``get_*`` / ``fetch_memory_*`` take a ``user_id`` and open their own admin
  connection, resolving the org via ``set_rls_context``. That resolution reads the
  user's org from the DB (TTL-cached), so it's for background/agent callers that
  have no Flask request context.
* ``fetch_entry_by_*`` take an already-open ``cursor`` plus an explicit
  ``org_id``. Request handlers must use these and pass the org from
  ``get_org_id_from_request()`` — the org RBAC authorized — rather than letting
  it be re-resolved from ``users.org_id``, which can lag behind while an org
  reassignment propagates.

  The handler must also pin RLS to that *same* org
  (``set_rls_context(..., org_id=org_id)``). These queries filter on ``org_id``
  in SQL, but RLS filters independently on ``myapp.current_org_id`` — if the two
  disagree the row is hidden and the query returns nothing, with no error.
"""

import logging
from typing import Dict, List, Optional

from utils.db.connection_pool import db_pool
from utils.auth.stateless_auth import set_rls_context

from services.memory import MEMORY_CATEGORIES, PROTECTED_ENTRIES

logger = logging.getLogger(__name__)

# Column list + row shape shared by every single-entry read, so the lookups below
# can differ only in their WHERE clause instead of restating all eight fields.
ENTRY_COLUMNS = """id, title, category, description, content,
                   last_edited_by, last_edited_by_name, updated_at"""


def serialize_entry(row) -> Dict:
    """Map an ENTRY_COLUMNS row to the single-entry API response shape."""
    return {
        "id": str(row[0]),
        "title": row[1],
        "category": row[2],
        "description": row[3],
        "content": row[4],
        "last_edited_by": row[5],
        "last_edited_by_name": row[6],
        "updated_at": row[7].isoformat() if row[7] else None,
        # Identity-locked entries (see PROTECTED_ENTRIES): content is editable,
        # but rename/recategorize/delete are refused. Surfaced so the client
        # doesn't have to mirror the registry.
        "is_protected": (row[2], row[1]) in PROTECTED_ENTRIES,
    }


def fetch_entry_by_id(cursor, org_id: str, entry_id: str) -> Optional[Dict]:
    """Read a single memory entry by id, or None if absent.

    Restricted to MEMORY_CATEGORIES so non-memory artifacts are never exposed
    through the memory API.
    """
    cursor.execute(
        f"""SELECT {ENTRY_COLUMNS} FROM artifacts
            WHERE id = %s AND org_id = %s AND category = ANY(%s)""",
        (entry_id, org_id, list(MEMORY_CATEGORIES)),
    )
    row = cursor.fetchone()
    return serialize_entry(row) if row else None


def fetch_entry_by_title(cursor, org_id: str, category: str, title: str) -> Optional[Dict]:
    """Read a single memory entry by (category, title), or None if absent.

    The by-title counterpart to ``fetch_entry_by_id``, for the well-known entries
    whose (category, title) pair is the stable identity that seeders and the
    agent's prompt injector pin to.
    """
    cursor.execute(
        f"""SELECT {ENTRY_COLUMNS} FROM artifacts
            WHERE org_id = %s AND category = %s AND title = %s""",
        (org_id, category, title),
    )
    row = cursor.fetchone()
    return serialize_entry(row) if row else None


def get_memory_content(user_id: str, category: str, title: str) -> Optional[str]:
    """Fetch a single artifact's raw content by (category, title).

    Plain DB read under RLS — the direct-Python counterpart to the agent
    `read_memory` tool (which returns a JSON envelope for LLM consumption).
    Returns None when the entry is absent or on any error, so callers can treat
    "missing" and "empty" the same way.
    """
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                org_id = set_rls_context(cursor, conn, user_id, log_prefix="[MemoryQueries:content]")
                if not org_id:
                    return None
                cursor.execute(
                    """SELECT content FROM artifacts
                       WHERE org_id = %s AND category = %s AND title = %s""",
                    (org_id, category, title),
                )
                row = cursor.fetchone()
                return (row[0] or "") if row else None
    except Exception:
        logger.exception("[MemoryQueries] Failed to fetch content for %s/%s", category, title)
        return None


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

                # Batch fetch all entries in a single query
                pairs = [(entry["category"], entry["title"]) for entry in entries]
                cursor.execute(
                    """SELECT category, title, content, updated_at FROM artifacts
                       WHERE org_id = %s AND (category, title) IN %s""",
                    (org_id, tuple(pairs)),
                )
                rows = cursor.fetchall()
                return [
                    {"category": row[0], "title": row[1], "content": row[2] or "", "updated_at": row[3]}
                    for row in rows
                ]
    except Exception:
        logger.exception("[MemoryQueries] Failed to fetch memory content")
        return []
