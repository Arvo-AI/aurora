"""
Memory Index Builder

Generates the MEMORY_INDEX content from all memory-category artifacts.
Injected into the agent's system prompt so it knows what org knowledge exists
and can call read_memory() to load specific topics on demand.

SCALING: Truncation here is a non-critical safeguard. In practice, the
relevance-ranking agent reads all entries and selects the top N
to inject, so the raw index size barely matters. Additionally, the
consolidation mechanism keeps memory lean by merging redundant entries.
Long-term, we can move to a directory-style index (e.g. infrastructure/prod,
infrastructure/staging) where the agent drills into relevant subdirectories.
"""

import logging
from typing import Optional

from utils.db.connection_pool import db_pool
from utils.auth.stateless_auth import set_rls_context

from services.memory import MEMORY_CATEGORIES

logger = logging.getLogger(__name__)

MAX_INDEX_LINES = 200
MAX_INDEX_BYTES = 25000


def build_memory_index(user_id: str, org_id: Optional[str] = None) -> str:
    """Build the memory index string for injection into the system prompt.

    Queries all memory-category artifacts for the org and formats them as a
    compact table of contents the agent can reference with read_memory().
    """
    if not user_id:
        return ""

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                resolved_org_id = set_rls_context(
                    cursor, conn, user_id, log_prefix="[MemoryIndex]"
                )
                if not resolved_org_id:
                    return ""

                cursor.execute(
                    """SELECT category, title, description, updated_at
                       FROM artifacts
                       WHERE org_id = %s AND category = ANY(%s)
                       ORDER BY category, updated_at DESC""",
                    (resolved_org_id, list(MEMORY_CATEGORIES)),
                )
                rows = cursor.fetchall()

        if not rows:
            return ""

        lines = [
            "ORG MEMORY — call read_memory(category, title) for full content:",
            "",
        ]

        current_category = None
        for category, title, description, updated_at in rows:
            if category != current_category:
                count = sum(1 for r in rows if r[0] == category)
                lines.append(f"## {category} ({count})")
                current_category = category

            desc_suffix = f"  # {description}" if description else ""
            lines.append(f"- read_memory('{category}', '{title}'){desc_suffix}")

        lines.append("")
        lines.append(
            "Use read_memory() to load full content when investigating relevant topics."
        )

        result = "\n".join(lines)

        # --- Budget enforcement (naive truncation — see module docstring for
        # improvement paths). Log when truncation happens so we can track how
        # often orgs hit this ceiling and prioritize a smarter strategy. ---
        truncated = False

        result_lines = result.split("\n")
        if len(result_lines) > MAX_INDEX_LINES:
            result = "\n".join(result_lines[:MAX_INDEX_LINES])
            result += "\n... (index truncated, use list_memories() to see all)"
            truncated = True

        if len(result.encode("utf-8")) > MAX_INDEX_BYTES:
            while len(result.encode("utf-8")) > MAX_INDEX_BYTES and "\n" in result:
                result = result.rsplit("\n", 1)[0]
            result += "\n... (index truncated)"
            truncated = True

        if truncated:
            logger.warning(
                "[MemoryIndex] Index truncated for org %s: %d entries, %d lines, %d bytes. "
                "Consider implementing tiered display or per-category limits.",
                resolved_org_id, len(rows), len(result.split("\n")), len(result.encode("utf-8")),
            )
        else:
            logger.debug(
                "[MemoryIndex] Built index for org %s: %d entries, %d bytes",
                resolved_org_id, len(rows), len(result.encode("utf-8")),
            )

        return result

    except Exception as e:
        logger.warning(f"[MemoryIndex] Error building index for user {user_id}: {e}")
        return ""
