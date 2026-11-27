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

    client = slack.SlackClient("token")
    with pytest.raises(slack.SlackRateLimited) as refused:
        client._make_request("GET", "conversations.history", {}, max_retries=0)
    assert refused.value.retry_after == 47
    assert isinstance(refused.value, ValueError)


def test_a_caller_that_waits_gets_the_answer_once_slack_gives_it(monkeypatch):
    answers = [_Response(429, {"Retry-After": "1"}), _Response(200, body={"ok": True, "messages": []})]
    monkeypatch.setattr(slack.requests, "get", lambda *_a, **_k: answers.pop(0))
    monkeypatch.setattr(slack.time, "sleep", lambda _s: None)

    assert slack.SlackClient("token")._make_request("GET", "conversations.history", {}) == {
        "ok": True, "messages": []}


def test_a_read_someone_waits_for_goes_ahead_of_the_chat_reader_and_tells_it_of_the_limit(monkeypatch):
    from connectors.slack_connector import read_limit
    from tests.services.detection.test_chat_reader import _Redis

    redis = _Redis()
    monkeypatch.setattr(read_limit, "get_redis_client", lambda: redis)
    reader_may_call = []

    def _slack(*_a, **_k):
        return answers.pop(0)

    def _while_waiting(_seconds):
        reader_may_call.append(read_limit.take("T1", read_limit.HISTORY))

    answers = [_Response(429, {"Retry-After": "20"}), _Response(200, body={"ok": True, "messages": []})]
    monkeypatch.setattr(slack.requests, "get", _slack)
    monkeypatch.setattr(slack.time, "sleep", _while_waiting)

    assert read_limit.take("T1", read_limit.HISTORY)
    slack.SlackClient("token", "T1")._make_request("GET", read_limit.HISTORY, {})

    assert read_limit.limited("T1", read_limit.HISTORY)
    assert reader_may_call == [False]
    # The answer was this minute's call: the reader waits its spacing, then has its turn again.
    assert not read_limit.take("T1", read_limit.HISTORY)
    redis.now += read_limit.SPACING_SECONDS + 1
    assert read_limit.take("T1", read_limit.HISTORY)


def test_a_read_that_gave_up_does_not_keep_the_chat_reader_out(monkeypatch):
    from connectors.slack_connector import read_limit
    from tests.services.detection.test_chat_reader import _Redis

    redis = _Redis()
    monkeypatch.setattr(read_limit, "get_redis_client", lambda: redis)
    monkeypatch.setattr(slack.requests, "get", lambda *_a, **_k: _Response(429, {"Retry-After": "5"}))
    monkeypatch.setattr(slack.time, "sleep", lambda _s: None)

    with pytest.raises(slack.SlackRateLimited):
        slack.SlackClient("token", "T1")._make_request("GET", read_limit.REPLIES, {}, max_retries=1)

    redis.now += read_limit.SPACING_SECONDS + 1
    assert read_limit.take("T1", read_limit.REPLIES)
