"""Post-investigation Jira comments run only when the org opted in."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

from routes.jira.jira_routes import JIRA_COMMENT_BACK_KEY, jira_comment_back_enabled


@pytest.mark.parametrize("stored,expected", [
    (None, False),
    (False, False),
    ("true", False),
    (True, True),
])
def test_comment_back_is_opt_in(monkeypatch, stored, expected):
    def _pref(user_id, key, default=None):
        assert key == JIRA_COMMENT_BACK_KEY
        return default if stored is None else stored

    monkeypatch.setattr("routes.jira.jira_routes.get_user_preference", _pref)
    assert jira_comment_back_enabled("uid-1") is expected


@pytest.fixture
def task(monkeypatch):
    """Import chat.background.task with Redis stubbed.

    The module builds a Redis client at import time and raises without one.
    """
    import utils.cache.redis_client as redis_client

    monkeypatch.setattr(redis_client, "get_redis_client", MagicMock)
    monkeypatch.delitem(sys.modules, "chat.background.task", raising=False)
    import chat.background.task as task_mod

    monkeypatch.delitem(sys.modules, "chat.background.task", raising=False)
    return task_mod


def test_filing_skips_when_comment_back_is_off(task, monkeypatch):
    monkeypatch.setattr(
        "routes.jira.jira_routes.jira_comment_back_enabled", lambda user_id: False,
    )
    called = {"n": 0}

    def _already_filed(*args, **kwargs):
        called["n"] += 1
        return False

    monkeypatch.setattr(task, "_session_has_successful_jira_action", _already_filed)
    ctx = {"integrations": {"jira": True}}
    assert task._should_file_in_jira(ctx, "sess-1", "uid-1") is False
    # Off means we never look up whether this session already posted.
    assert called["n"] == 0


def test_filing_runs_when_comment_back_is_on(task, monkeypatch):
    monkeypatch.setattr(
        "routes.jira.jira_routes.jira_comment_back_enabled", lambda user_id: True,
    )
    monkeypatch.setattr(task, "_session_has_successful_jira_action", lambda *a, **k: False)
    ctx = {"integrations": {"jira": True}}
    assert task._should_file_in_jira(ctx, "sess-1", "uid-1") is True


def test_filing_skips_without_jira(task, monkeypatch):
    monkeypatch.setattr(
        "routes.jira.jira_routes.jira_comment_back_enabled", lambda user_id: True,
    )
    assert task._should_file_in_jira({"integrations": {}}, "sess-1", "uid-1") is False
    assert task._should_file_in_jira(None, "sess-1", "uid-1") is False
