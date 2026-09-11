"""
Memory Index Builder

Generates the memory index for injection into the agent's system prompt.
Lists all memory entries with title + description so the agent knows what
org knowledge exists and can call read_memory() to load specific topics.
"""

import logging

from services.memory.queries import get_memory_entries

logger = logging.getLogger(__name__)

MAX_INDEX_LINES = 200
MAX_INDEX_BYTES = 25000


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
