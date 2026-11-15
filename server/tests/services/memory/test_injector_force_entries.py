"""MemoryPrefetch force_entries: source-specific memories always inject."""

import asyncio
from datetime import datetime
from unittest.mock import patch

from services.memory.injector import MemoryPrefetch


_ENTRIES = [
    {"category": "context", "title": "Slack", "description": "slack policy", "updated_at": datetime(2024, 1, 1)},
    {"category": "runbook", "title": "Redis", "description": "redis runbook", "updated_at": datetime(2024, 1, 1)},
]


def _run(coro):
    return asyncio.run(coro)


def test_forced_entry_injected_even_when_selector_picks_nothing():
    pf = MemoryPrefetch(
        user_id="u1",
        session_id="s1",
        user_message="hello",
        force_entries=[("context", "Slack")],
    )

    def fake_fetch(user_id, entries):
        # Echo back content for whatever was selected.
        return [
            {"category": e["category"], "title": e["title"], "content": f"content-{e['title']}",
             "updated_at": e["updated_at"]}
            for e in entries
        ]

    with patch("services.memory.injector._get_surfaced_set", return_value=set()), \
         patch("services.memory.injector.get_memory_entries", return_value=_ENTRIES), \
         patch("services.memory.injector._select_relevant_memories_async", return_value=[]), \
         patch("services.memory.injector.fetch_memory_content", side_effect=fake_fetch), \
         patch("services.memory.injector._mark_surfaced"):
        result = _run(pf._execute())

    # The forced "Slack" entry is present despite the selector returning [].
    assert "context/Slack" in result
    assert "content-Slack" in result
    # The non-forced entry the selector didn't pick is absent.
    assert "runbook/Redis" not in result


def test_forced_entry_not_duplicated_when_selector_also_picks_it():
    pf = MemoryPrefetch(
        user_id="u1",
        session_id="s1",
        user_message="hello",
        force_entries=[("context", "Slack")],
    )

    def fake_fetch(user_id, entries):
        return [
            {"category": e["category"], "title": e["title"], "content": f"content-{e['title']}",
             "updated_at": e["updated_at"]}
            for e in entries
        ]

    # Selector should never see the forced entry (it's excluded from the pool),
    # so it can't double it. Assert the pool excludes it.
    captured = {}

    async def fake_select(msg, entries):
        captured["pool"] = [f"{e['category']}/{e['title']}" for e in entries]
        return []

    with patch("services.memory.injector._get_surfaced_set", return_value=set()), \
         patch("services.memory.injector.get_memory_entries", return_value=_ENTRIES), \
         patch("services.memory.injector._select_relevant_memories_async", side_effect=fake_select), \
         patch("services.memory.injector.fetch_memory_content", side_effect=fake_fetch), \
         patch("services.memory.injector._mark_surfaced"):
        result = _run(pf._execute())

    assert "context/Slack" not in captured["pool"]
    # Appears exactly once.
    assert result.count("context/Slack") == 1
