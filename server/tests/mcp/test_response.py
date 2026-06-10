"""Tests for the shared MCP response helpers."""

from __future__ import annotations

from aurora_mcp import response


def test_small_payload_passes_through():
    payload = {"items": [{"id": i} for i in range(5)]}
    assert response.truncate_payload(payload) == payload


def test_large_payload_passes_through_unchanged():
    """Large payloads are returned as-is — no truncation, no broken JSON."""
    big = "x" * 100_000
    out = response.truncate_payload({"blob": big}, tool_name="t1")
    assert out == {"blob": big}


def test_paginate_returns_next_cursor_when_more_remain():
    items = list(range(50))
    page, cursor = response.paginate(items, limit=10)
    assert page == list(range(10))
    assert cursor == "10"


def test_paginate_returns_none_cursor_at_end():
    items = list(range(5))
    page, cursor = response.paginate(items, limit=10)
    assert page == items
    assert cursor is None


def test_paginate_clamps_limit():
    items = list(range(500))
    page, _ = response.paginate(items, limit=10_000)
    assert len(page) == 100  # max_limit default


def test_paginate_handles_invalid_cursor():
    items = list(range(5))
    page, _ = response.paginate(items, cursor="not-a-number", limit=10)
    assert page == items


def test_wrap_listing_shape():
    out = response.wrap_listing([1, 2, 3], limit=2)
    assert out["items"] == [1, 2]
    assert out["next_cursor"] == "2"
    assert out["total"] == 3


def test_list_payload_is_coerced_to_dict():
    """Real bug surfaced by live testing: when a backend route returns a JSON
    array (e.g. /datadog/monitors) FastMCP's `Dict[str, Any]` return-type
    validation rejects it. truncate_payload must wrap non-dicts in
    `{"items": ...}` so every MCP tool's contract is honored."""
    out = response.truncate_payload([{"a": 1}, {"a": 2}])
    assert isinstance(out, dict)
    assert out["items"] == [{"a": 1}, {"a": 2}]


def test_scalar_payload_is_coerced_to_dict():
    out = response.truncate_payload(42)
    assert isinstance(out, dict)
    assert out["items"] == 42
