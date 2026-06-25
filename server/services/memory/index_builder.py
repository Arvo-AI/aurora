"""
Memory Index Builder

Generates the memory index for injection into the agent's system prompt.
Lists all memory entries with title + description so the agent knows what
org knowledge exists and can call read_memory() to load specific topics.

Future: A relevance-ranking agent will decide which memories to load fully
into the prompt based on the current conversation context.

SCALING: Truncation is a non-critical safeguard. The relevance-ranking agent
reads all entries and selects the top N, and the consolidation mechanism keeps
memory lean. Long-term, we can move to a directory-style index.
"""

import logging

from utils.db.connection_pool import db_pool
from utils.auth.stateless_auth import set_rls_context

from services.memory import MEMORY_CATEGORIES

logger = logging.getLogger(__name__)

MAX_INDEX_LINES = 200
MAX_INDEX_BYTES = 25000


def build_memory_index(user_id: str) -> str:
    """Build a table-of-contents index of all org memory entries.

    Returns a formatted string listing every entry by category with its
    description. The agent uses this to discover what knowledge exists and
    calls read_memory() to load full content on demand.
    """
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                resolved_org_id = set_rls_context(
                    cursor, conn, user_id, log_prefix="[MemoryIndex]"
                )
                if not resolved_org_id:
                    return ""

                # Fetch all memory entries for this org (title + description only, no content)
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

        # Format as a grouped list: entries under category headers
        lines = ["ORG MEMORY INDEX — use read_memory(category, title) for full content:\n"]

        current_category = None
        for category, title, description, updated_at in rows:
            # Print a category header when we enter a new group
            if category != current_category:
                count = sum(1 for r in rows if r[0] == category)
                lines.append(f"## {category} ({count})")
                current_category = category

            # Each entry: path-style identifier + description as inline comment
            desc_suffix = f"  # {description}" if description else ""
            lines.append(f"- {category}/{title}{desc_suffix}")

        result = "\n".join(lines)

        # Enforce line budget — prevent the index from consuming too much prompt space
        truncated = False
        result_lines = result.split("\n")
        if len(result_lines) > MAX_INDEX_LINES:
            result = "\n".join(result_lines[:MAX_INDEX_LINES])
            result += "\n... (index truncated — use list_memories() to see all)"
            truncated = True

        # Enforce byte budget — safety net for orgs with many long titles/descriptions
        if len(result.encode("utf-8")) > MAX_INDEX_BYTES:
            while len(result.encode("utf-8")) > MAX_INDEX_BYTES and "\n" in result:
                result = result.rsplit("\n", 1)[0]
            result += "\n... (index truncated)"
            truncated = True

        if truncated:
            logger.warning(
                "[MemoryIndex] Index truncated for org %s: %d entries",
                resolved_org_id, len(rows),
            )

        # LangChain interprets {text} as template variables — escape them
        result = result.replace("{", "{{").replace("}", "}}")

        return result

    except Exception as e:
        logger.warning(f"[MemoryIndex] Error building index for user {user_id}: {e}")
        return ""
