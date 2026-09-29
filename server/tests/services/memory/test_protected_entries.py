"""PROTECTED_ENTRIES: well-known memory entries keep a stable (category, title).

The Slack policy lives in a user-writable category on purpose (both the user and
the agent edit its content), but the agent's prompt injector and the seeder pin to
its (category, title) pair. Renaming/recategorizing/deleting it would silently
detach it — no error, Aurora just stops applying the policy — so the memory routes
reject those operations. These tests lock that contract in.
"""

from services.memory import (
    PROTECTED_ENTRIES,
    SLACK_MEMORY_CATEGORY,
    SLACK_MEMORY_TITLE,
    SYSTEM_CATEGORY,
    USER_WRITABLE_CATEGORIES,
)


def test_slack_memory_is_protected():
    assert (SLACK_MEMORY_CATEGORY, SLACK_MEMORY_TITLE) in PROTECTED_ENTRIES


def test_slack_memory_stays_user_writable():
    # Protection must NOT be implemented by moving it into the system category —
    # users are meant to edit the policy's content freely.
    assert SLACK_MEMORY_CATEGORY in USER_WRITABLE_CATEGORIES
    assert SLACK_MEMORY_CATEGORY != SYSTEM_CATEGORY


def test_protected_entries_are_category_title_pairs():
    # The route guards do `(category, title) in PROTECTED_ENTRIES`, so every member
    # must be a 2-tuple or those checks silently never match.
    for item in PROTECTED_ENTRIES:
        assert isinstance(item, tuple) and len(item) == 2, item
        category, title = item
        assert isinstance(category, str) and category
        assert isinstance(title, str) and title


def test_seeder_uses_the_protected_identity():
    """The seeded row must land on exactly the protected (category, title)."""
    from services.memory import slack_memory

    assert slack_memory.SLACK_MEMORY_CATEGORY == SLACK_MEMORY_CATEGORY
    assert slack_memory.SLACK_MEMORY_TITLE == SLACK_MEMORY_TITLE


def test_agent_forces_the_protected_identity():
    """The injector's force_entries key must match the protected pair.

    Guards against the constants drifting from the literal the injector builds:
    a mismatch here means Slack sessions silently stop getting the policy.
    """
    from services.memory.injector import _entry_key

    forced_key = f"{SLACK_MEMORY_CATEGORY}/{SLACK_MEMORY_TITLE}"
    entry = {"category": SLACK_MEMORY_CATEGORY, "title": SLACK_MEMORY_TITLE}
    assert _entry_key(entry) == forced_key
