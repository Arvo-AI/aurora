"""
Memory Index Builder

Generates the memory index for injection into the agent's system prompt.
Lists all memory entries with title + description so the agent knows what
org knowledge exists and can call read_memory() to load specific topics.

Also provides shared data-access functions used by the injector.
"""

import logging
from typing import Dict, List, Optional

from utils.db.connection_pool import db_pool
from utils.auth.stateless_auth import set_rls_context

from services.memory import MEMORY_CATEGORIES

logger = logging.getLogger(__name__)

MAX_INDEX_LINES = 200
MAX_INDEX_BYTES = 25000


# ---------------------------------------------------------------------------
# Shared data access — used by both the index builder and the injector
# ---------------------------------------------------------------------------


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
        logger.warning("[MemoryIndex] Failed to fetch entries for user %s: %s", user_id, e)
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
                org_id = set_rls_context(cursor, conn, user_id, log_prefix="[MemoryIndex:fetch]")
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
        logger.exception("[MemoryIndex] Failed to fetch memory content")
        return []


# ---------------------------------------------------------------------------
# Index formatting (used by composer.py for the static TOC)
# ---------------------------------------------------------------------------


def build_memory_index(user_id: str) -> str:
    """Build a table-of-contents index of all org memory entries.

    Returns a formatted string listing every entry by category with its
    description. The agent uses this to discover what knowledge exists and
    calls read_memory() to load full content on demand.
    """
    entries = get_memory_entries(user_id)
    if not entries:
        return ""

    # Regroup by category (entries come sorted by updated_at, need category grouping)
    from itertools import groupby
    sorted_entries = sorted(entries, key=lambda e: e["category"])

    lines = ["ORG MEMORY INDEX — use read_memory(category, title) for full content:\n"]

    def _prompt_label(value: object, max_chars: int = 500) -> str:
        return " ".join(str(value or "").split())[:max_chars]

    for category, group in groupby(sorted_entries, key=lambda e: e["category"]):
        group_list = list(group)
        lines.append(f"## {category} ({len(group_list)})")

        for entry in group_list:
            safe_title = _prompt_label(entry["title"])
            safe_desc = _prompt_label(entry["description"])
            desc_suffix = f"  # {safe_desc}" if safe_desc else ""
            lines.append(f"- {category}/{safe_title}{desc_suffix}")

    result = "\n".join(lines)

    # Enforce line budget
    truncated = False
    result_lines = result.split("\n")
    if len(result_lines) > MAX_INDEX_LINES:
        result = "\n".join(result_lines[:MAX_INDEX_LINES])
        result += "\n... (index truncated — use list_memories() to see all)"
        truncated = True

    # Enforce byte budget
    if len(result.encode("utf-8")) > MAX_INDEX_BYTES:
        while len(result.encode("utf-8")) > MAX_INDEX_BYTES and "\n" in result:
            result = result.rsplit("\n", 1)[0]
        result += "\n... (index truncated)"
        truncated = True

    if truncated:
        logger.warning("[MemoryIndex] Index truncated: %d entries", len(entries))

    # LangChain interprets {text} as template variables — escape them
    result = result.replace("{", "{{").replace("}", "}}")

    return result
