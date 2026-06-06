"""Shared response helpers for MCP tools."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple


def truncate_payload(payload: Any, **_kwargs: Any) -> Dict[str, Any]:
    """Guarantee a dict return shape for MCP tool responses.

    MCP tools are declared with a `Dict[str, Any]` return type. FastMCP
    validates that contract — if a backend route returns a bare JSON array
    (e.g. /datadog/monitors), Pydantic raises "Input should be a valid
    dictionary" and the tool call fails. Wrap any non-dict payload in
    `{"items": payload}` so the contract always holds.
    """
    if not isinstance(payload, dict):
        payload = {"items": payload}
    return payload


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
    items: List[Any], *, cursor: Optional[str] = None, limit: int = 20,
) -> Dict[str, Any]:
    """Standard envelope for list-returning tools: {items, next_cursor, total}."""
    page, next_cursor = paginate(items, cursor=cursor, limit=limit)
    return truncate_payload(
        {"items": page, "next_cursor": next_cursor, "total": len(items)},
    )
