"""Shared response helpers for MCP tools.

Wraps `utils.tool_output_cap` so list-returning tools enforce the 15k-token
cap consistently, plus a small cursor-paginate helper.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Roughly 15k tokens at 4 chars/token; aligned with PASS_THROUGH_CHARS in
# chat.backend.agent.utils.tool_output_cap (40_000 chars ≈ 10–12k tokens; we
# go slightly higher because MCP doesn't run an LLM summarizer in-process).
RESPONSE_HARD_CHAR_CAP = 60_000


def truncate_payload(payload: Any, *, tool_name: str = "mcp") -> Any:
    """Hard cap a tool response to RESPONSE_HARD_CHAR_CAP chars when serialized.

    If the payload is small enough, returns it unchanged. Otherwise returns a
    dict shaped { "truncated": true, "tool": ..., "summary": "<head bytes>",
    "original_size": N } so the caller knows the response was clipped.
    """
    try:
        encoded = json.dumps(payload, default=str)
    except (TypeError, ValueError):
        encoded = str(payload)

    if len(encoded) <= RESPONSE_HARD_CHAR_CAP:
        return payload

    head = encoded[: RESPONSE_HARD_CHAR_CAP - 200]
    return {
        "truncated": True,
        "tool": tool_name,
        "original_size_chars": len(encoded),
        "max_size_chars": RESPONSE_HARD_CHAR_CAP,
        "summary": head + "\n...[truncated]",
        "hint": (
            "Result exceeded the MCP per-call size cap. Narrow your query "
            "(shorter time range, smaller limit) or use a resource URI to "
            "fetch the full payload."
        ),
    }


def paginate(
    items: Iterable[Any],
    *,
    cursor: Optional[str] = None,
    limit: int = 20,
    max_limit: int = 100,
) -> Tuple[List[Any], Optional[str]]:
    """Slice `items` by an integer-offset cursor and return (page, next_cursor)."""
    limit = max(1, min(int(limit or 20), max_limit))
    try:
        start = int(cursor) if cursor else 0
    except (TypeError, ValueError):
        start = 0
    materialized = list(items)
    page = materialized[start : start + limit]
    next_cursor = str(start + limit) if (start + limit) < len(materialized) else None
    return page, next_cursor


def wrap_listing(
    items: List[Any], *, cursor: Optional[str] = None, limit: int = 20, tool_name: str = "mcp"
) -> Dict[str, Any]:
    """Standard envelope for list-returning tools: {items, next_cursor, total}."""
    page, next_cursor = paginate(items, cursor=cursor, limit=limit)
    return truncate_payload(
        {"items": page, "next_cursor": next_cursor, "total": len(items)},
        tool_name=tool_name,
    )
