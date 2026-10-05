"""The post-investigation Jira filing step only runs when the org opted in.

This is the step that produced the reported behaviour: with Jira merely
connected, every completed RCA posted a comment under the connecting user's
Atlassian name.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

from connectors.jira_connector import settings


@pytest.fixture
def task(monkeypatch):
    """Import chat.background.task with Redis stubbed.

    The module builds a Redis client at import time and raises without one.
    """
    import utils.cache.redis_client as redis_client

    monkeypatch.setattr(redis_client, "get_redis_client", MagicMock)
    monkeypatch.delitem(sys.modules, "chat.background.task", raising=False)
    import chat.background.task as task_mod

    # Don't leave the Redis-stubbed module cached for later tests.
    monkeypatch.delitem(sys.modules, "chat.background.task", raising=False)
    return task_mod


def _rca_context(mode=None, jira=True):
    integrations = {}
    if jira:
        integrations["jira"] = True
    if mode is not None:
        integrations["jira_mode"] = mode
    return {"source": "datadog", "integrations": integrations}


@pytest.fixture
def filed(task, monkeypatch):
    """_should_file_in_jira with the "already filed" DB check stubbed out."""
    monkeypatch.setattr(task, "_session_has_successful_jira_action", lambda *a, **k: False)
    return lambda ctx: task._should_file_in_jira(ctx, "sess-1", "uid-1")


def test_connected_but_unset_mode_does_not_file(filed):
    """The regression: Jira connected as a context source, nothing opted in."""
    assert filed(_rca_context(mode=None)) is False


def test_read_only_does_not_file(filed):
    assert filed(_rca_context(mode=settings.READ_ONLY)) is False


@pytest.mark.parametrize("mode", [settings.COMMENT_ONLY, settings.FULL])
def test_opted_in_modes_file(filed, mode):
    assert filed(_rca_context(mode=mode)) is True


def test_jira_not_connected_never_files(filed):
    assert filed(_rca_context(mode=settings.FULL, jira=False)) is False


def test_missing_rca_context_never_files(filed):
    assert filed(None) is False


def test_already_filed_in_session_does_not_file_again(task, monkeypatch):
    """The agent can comment mid-investigation; Phase 2 must not double-post."""
    monkeypatch.setattr(task, "_session_has_successful_jira_action", lambda *a, **k: True)
    assert task._should_file_in_jira(_rca_context(mode=settings.FULL), "sess-1", "uid-1") is False


def test_read_only_skips_the_already_filed_lookup(task, monkeypatch):
    """A read-only org shouldn't pay for a DB round trip to learn it can't post."""
    def _explode(*_a, **_kw):
        raise AssertionError("queried the session despite read-only Jira")

    monkeypatch.setattr(task, "_session_has_successful_jira_action", _explode)
    assert task._should_file_in_jira(_rca_context(mode=settings.READ_ONLY), "sess-1", "uid-1") is False


def test_rca_context_carries_the_mode(task, monkeypatch):
    """Phase 2 and the skill prompt read the mode off integrations, so it has
    to be attached there — it was absent before, which pinned every org to the
    old comment_only default."""
    monkeypatch.setattr(task, "_get_connected_integrations", lambda user_id: {"jira": True})
    monkeypatch.setattr(task, "get_jira_mode", lambda user_id: settings.FULL)
    monkeypatch.setattr(
        "utils.auth.stateless_auth.get_connected_providers", lambda user_id: ["aws"]
    )

    ctx = task._build_rca_context("uid-1", trigger_metadata={"source": "datadog"})

    assert ctx["integrations"]["jira_mode"] == settings.FULL
    # The mode is not an integration; it must not leak into the provider list.
    assert "jira_mode" not in ctx["providers"]
