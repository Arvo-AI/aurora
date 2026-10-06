"""Jira write tools refuse when comment-back is off (even if cached)."""

from __future__ import annotations

import pytest

from chat.backend.agent.tools import jira_tool


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
def test_write_tools_blocked_when_comment_back_off(monkeypatch, fn, args):
    monkeypatch.setattr(
        "routes.jira.jira_routes.jira_comment_back_enabled",
        lambda user_id: False,
    )
    with pytest.raises(ValueError, match="comment-back is disabled"):
        fn(*args, user_id="uid-1")
