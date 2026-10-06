"""Jira write tools refuse when comment-back is off (even if cached)."""

from __future__ import annotations

import pytest

from chat.backend.agent.tools import jira_tool


def test_add_comment_blocked_when_comment_back_off(monkeypatch):
    monkeypatch.setattr(
        "routes.jira.jira_routes.jira_comment_back_enabled",
        lambda user_id: False,
    )
    with pytest.raises(ValueError, match="comment-back is disabled"):
        jira_tool.jira_add_comment("OPS-1", "hello", user_id="uid-1")
