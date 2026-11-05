"""BACKGROUND_CHAT_RATE_LIMIT_MAX parsing and the 5-minute intake counter."""

from chat.intake_limit import (
    DEFAULT_MAX_REQUESTS,
    MAX_MAX_REQUESTS,
    MIN_MAX_REQUESTS,
    WINDOW_SECONDS,
    background_chat_rate_limit_max,
    consume_background_chat_slot,
)


class _FakeRedis:
    def __init__(self):
        self.counts = {}
        self.ttls = {}
        self.expire_calls = []

    def incr(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    def expire(self, key, seconds):
        self.expire_calls.append((key, seconds))
        self.ttls[key] = seconds

    def ttl(self, key):
        return self.ttls.get(key, -1)


def test_default_is_twenty_per_window(monkeypatch):
    monkeypatch.delenv("BACKGROUND_CHAT_RATE_LIMIT_MAX", raising=False)
    assert background_chat_rate_limit_max() == DEFAULT_MAX_REQUESTS
    assert background_chat_rate_limit_max() == 20


def test_blank_uses_the_default(monkeypatch):
    monkeypatch.setenv("BACKGROUND_CHAT_RATE_LIMIT_MAX", "  ")
    assert background_chat_rate_limit_max() == 20


def test_custom_value(monkeypatch):
    monkeypatch.setenv("BACKGROUND_CHAT_RATE_LIMIT_MAX", " 120 ")
    assert background_chat_rate_limit_max() == 120


def test_non_integer_falls_back(monkeypatch):
    monkeypatch.setenv("BACKGROUND_CHAT_RATE_LIMIT_MAX", "lots")
    assert background_chat_rate_limit_max() == 20


def test_below_minimum_is_clamped(monkeypatch):
    monkeypatch.setenv("BACKGROUND_CHAT_RATE_LIMIT_MAX", "0")
    assert background_chat_rate_limit_max() == MIN_MAX_REQUESTS
    monkeypatch.setenv("BACKGROUND_CHAT_RATE_LIMIT_MAX", "-5")
    assert background_chat_rate_limit_max() == 1


def test_above_maximum_is_clamped(monkeypatch):
    monkeypatch.setenv("BACKGROUND_CHAT_RATE_LIMIT_MAX", "999999")
    assert background_chat_rate_limit_max() == MAX_MAX_REQUESTS


def test_python_int_syntax(monkeypatch):
    monkeypatch.setenv("BACKGROUND_CHAT_RATE_LIMIT_MAX", "+90")
    assert background_chat_rate_limit_max() == 90
    monkeypatch.setenv("BACKGROUND_CHAT_RATE_LIMIT_MAX", "1_000")
    assert background_chat_rate_limit_max() == 1000


def test_window_allows_the_configured_cap_then_rejects(monkeypatch):
    monkeypatch.setenv("BACKGROUND_CHAT_RATE_LIMIT_MAX", "3")
    redis = _FakeRedis()
    assert consume_background_chat_slot("uid-a", redis) is True
    assert consume_background_chat_slot("uid-a", redis) is True
    assert consume_background_chat_slot("uid-a", redis) is True
    assert consume_background_chat_slot("uid-a", redis) is False
    # A different user does not share the counter.
    assert consume_background_chat_slot("uid-b", redis) is True


def test_ttl_is_set_only_on_the_first_hit(monkeypatch):
    monkeypatch.setenv("BACKGROUND_CHAT_RATE_LIMIT_MAX", "5")
    redis = _FakeRedis()
    consume_background_chat_slot("uid-a", redis)
    consume_background_chat_slot("uid-a", redis)
    assert redis.expire_calls == [("background_chat_rate_limit:uid-a", WINDOW_SECONDS)]
    assert WINDOW_SECONDS == 300
