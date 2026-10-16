"""list_slack_users returns mention tokens for a name prefix, not the workspace."""

import json
from unittest.mock import patch

import chat.backend.agent.tools.slack_tool as slack_tool


def _member(uid, real="", display="", *, bot=False, deleted=False):
    return {
        "id": uid,
        "deleted": deleted,
        "is_bot": bot,
        "real_name": real,
        "profile": {"display_name": display, "real_name": real},
    }


class _UsersClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def _make_request(self, method, endpoint, params):
        self.calls.append((endpoint, dict(params)))
        index = len(self.calls) - 1
        cursor = "page-2" if index + 1 < len(self.pages) else ""
        return {
            "ok": True,
            "members": self.pages[index],
            "response_metadata": {"next_cursor": cursor},
        }


def _run(client, name="a"):
    with patch.object(slack_tool, "get_slack_client_for_user", return_value=client):
        return json.loads(slack_tool.list_slack_users(name=name, user_id="u1"))


def test_prefix_matches_first_name_and_returns_a_mention():
    client = _UsersClient([[
        _member("UADA", "Ada Lovelace"),
        _member("UALEX", display="alex"),
        _member("UBOB", "Bob"),
        _member("UBOT", "Aurora", bot=True),
        _member("UANN", "Ann", deleted=True),
        _member("USLACKBOT", "Slackbot"),
    ]])
    out = _run(client, "a")
    mentions = [user["mention"] for user in out["users"]]
    assert mentions == ["<@UADA>", "<@UALEX>"]
    assert out["total"] == 2
    assert "truncated" not in out


def test_full_name_prefix_narrows_to_one_person():
    client = _UsersClient([[
        _member("UADA", "Ada Lovelace"),
        _member("UALEX", "Alex"),
    ]])
    out = _run(client, "ada")
    assert out["users"] == [{
        "mention": "<@UADA>",
        "display_name": "",
        "real_name": "Ada Lovelace",
    }]


def test_second_page_is_scanned_when_the_first_has_no_match():
    client = _UsersClient([
        [_member("UBOB", "Bob")],
        [_member("UADA", "Ada Lovelace")],
    ])
    out = _run(client, "ada")
    assert out["users"][0]["mention"] == "<@UADA>"
    assert client.calls[1][1]["cursor"] == "page-2"


def test_empty_prefix_is_rejected_before_slack():
    client = _UsersClient([[_member("UADA", "Ada")]])
    out = _run(client, "  ")
    assert "prefix" in out["error"]
    assert client.calls == []


def test_not_connected_returns_error():
    with patch.object(slack_tool, "get_slack_client_for_user", return_value=None):
        out = json.loads(slack_tool.list_slack_users(name="a", user_id="u1"))
    assert "error" in out


def test_page_cap_reports_the_list_is_partial():
    # One page past the cap, so Slack still has a cursor when we stop.
    pages = [[_member("UBOB", "Bob")] for _ in range(slack_tool._USER_LIST_MAX_PAGES + 1)]
    client = _UsersClient(pages)
    out = _run(client, "a")
    assert out["users"] == []
    assert out["truncated"] is True
    assert len(client.calls) == slack_tool._USER_LIST_MAX_PAGES


def test_result_cap_reports_the_list_is_partial(monkeypatch):
    monkeypatch.setattr(slack_tool, "_USER_LIST_MAX_RESULTS", 1)
    client = _UsersClient([[
        _member("UADA", "Ada"),
        _member("UALEX", "Alex"),
    ]])
    out = _run(client, "a")
    assert [user["mention"] for user in out["users"]] == ["<@UADA>"]
    assert out["truncated"] is True
