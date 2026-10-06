"""Jira write tools refuse unattended posts when comment-back is off (even if cached)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from chat.backend.agent.tools import jira_tool


def _set_session(monkeypatch, state):
    monkeypatch.setattr(
        "chat.backend.agent.tools.cloud_tools.get_state_context",
        lambda: state,
    )


@pytest.fixture
def comment_back_off(monkeypatch):
    monkeypatch.setattr(
        "routes.jira.jira_routes.jira_comment_back_enabled",
        lambda user_id: False,
    )


# A cached tool list can still offer these after the org turns comment-back off.
# Each one has to refuse on its own, before it talks to Jira.
@pytest.mark.parametrize(
    "fn,args",
    [
        (jira_tool.jira_add_comment, ("OPS-1", "hello")),
        (jira_tool.jira_create_issue, ("OPS", "summary")),
        (jira_tool.jira_update_issue, ("OPS-1", {"summary": "x"})),
        (jira_tool.jira_link_issues, ("OPS-1", "OPS-2")),
    ],
)
@pytest.mark.parametrize(
    "state",
    [None, SimpleNamespace(is_background=True)],
    ids=["no-session", "background"],
)
def test_write_tools_blocked_when_comment_back_off(monkeypatch, comment_back_off, fn, args, state):
    _set_session(monkeypatch, state)
    with pytest.raises(ValueError, match="comment-back is disabled"):
        fn(*args, user_id="uid-1")


def test_interactive_chat_can_write_when_comment_back_off(monkeypatch, comment_back_off):
    _set_session(monkeypatch, SimpleNamespace(is_background=False))
    jira_tool._require_jira_comment_back("uid-1")
