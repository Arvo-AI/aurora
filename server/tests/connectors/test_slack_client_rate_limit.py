"""A 429 from Slack is told apart from a request that failed."""

import pytest

from connectors.slack_connector import client as slack


class _Response:
    def __init__(self, status, headers=None, body=None):
        self.status_code, self.headers, self._body = status, headers or {}, body or {"ok": True}

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


def test_a_caller_that_will_not_wait_is_told_how_long_slack_asked_for(monkeypatch):
    monkeypatch.setattr(slack.requests, "get", lambda *_a, **_k: _Response(429, {"Retry-After": "47"}))
    monkeypatch.setattr(slack.time, "sleep", lambda _s: pytest.fail("it waited"))

    with pytest.raises(slack.SlackRateLimited) as refused:
        slack.SlackClient("token")._make_request("GET", "conversations.history", {}, max_retries=0)
    assert refused.value.retry_after == 47
    assert isinstance(refused.value, ValueError)


def test_a_caller_that_waits_gets_the_answer_once_slack_gives_it(monkeypatch):
    answers = [_Response(429, {"Retry-After": "1"}), _Response(200, body={"ok": True, "messages": []})]
    monkeypatch.setattr(slack.requests, "get", lambda *_a, **_k: answers.pop(0))
    monkeypatch.setattr(slack.time, "sleep", lambda _s: None)

    assert slack.SlackClient("token")._make_request("GET", "conversations.history", {}) == {
        "ok": True, "messages": []}
