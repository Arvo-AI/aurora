"""What one turn of the chat reader does with a channel, whatever Slack and the model do."""

import contextlib
import random

import pytest

from connectors.slack_connector import read_limit
from connectors.slack_connector.client import SlackAPIError
from services.detection import chat_intake, chat_pace, chat_signals
from tests.services.detection.test_chat_reader import _Redis

_TURN = chat_signals.Turn("C1", "1795100200.000000", "listen", "claim", {})
_WORKSPACE = chat_intake._Workspace("T0OURS", "UBOT", "user", "org")
_MESSAGE = {"ts": "1795100300.000000", "text": "search is down"}


def _slack_gives(messages):
    def gather(*_args):
        return messages, [], messages[-1]["ts"]
    return gather


def _slack_raises(error, answered_first=False):
    def gather(*args):
        if answered_first:
            args[-1].answers["conversations.history {}"] = {"messages": [_MESSAGE]}
        raise error
    return gather


def _model_raises(*_args, **_kwargs):
    raise RuntimeError("the provider refused")


@pytest.fixture
def turn(monkeypatch):
    """Run one turn over a channel; returns what became of the channel and what was kept in Redis."""
    redis = _Redis()
    monkeypatch.setattr(read_limit, "get_redis_client", lambda: redis)
    became = []

    @contextlib.contextmanager
    def _cursor(_user, _org):
        yield object(), type("Conn", (), {"commit": lambda self: None})()

    monkeypatch.setattr(chat_intake, "_org_cursor", _cursor)
    monkeypatch.setattr(chat_signals, "record_read", lambda *_a: None)
    monkeypatch.setattr(chat_signals, "advance", lambda *_a: became.append("read"))
    monkeypatch.setattr(chat_signals, "read_failed", lambda *_a: became.append("failure counted") or False)
    monkeypatch.setattr(chat_signals, "put_back",
                        lambda _c, _o, _ch, _claim, first=False: became.append("put back first" if first else "put back"))

    def run(gather, model=lambda *_a, **_k: []):
        monkeypatch.setattr(chat_intake, "_gather", gather)
        monkeypatch.setattr(chat_intake.chat_reader, "read", model)
        chat_intake._read_channel(object(), _WORKSPACE, _TURN, [])
        kept = redis.get("detection:chat:progress:org:C1") is not None
        return became, kept

    return run


def test_a_channel_that_was_read_moves_on_and_keeps_nothing(turn):
    assert turn(_slack_gives([_MESSAGE])) == (["read"], False)


@pytest.mark.parametrize("slack, became", [
    (_slack_raises(chat_pace.Busy()), ["put back"]),
    (_slack_raises(chat_pace.Busy(), answered_first=True), ["put back first"]),
    (_slack_raises(chat_pace.Unreachable(), answered_first=True), ["put back"]),
])
def test_a_channel_slack_could_not_be_asked_about_now_is_put_back_with_nothing_counted_against_it(
        turn, slack, became):
    channel, _kept = turn(slack)
    assert channel == became


def test_what_slack_had_answered_is_kept_for_the_turn_that_goes_on_with_the_reading(turn):
    _channel, kept = turn(_slack_raises(chat_pace.Busy(), answered_first=True))
    assert kept


def test_a_channel_slack_refuses_to_show_is_counted_as_a_failure(turn):
    channel, _kept = turn(_slack_raises(SlackAPIError("not_in_channel")))
    assert channel == ["failure counted"]


def test_a_model_that_fails_is_counted_and_slack_is_not_asked_again_for_the_same_messages(turn):
    def gather(*args):
        args[-1].answers["conversations.history {}"] = {"messages": [_MESSAGE]}
        return [_MESSAGE], [], _MESSAGE["ts"]

    assert turn(gather, _model_raises) == (["failure counted"], True)


def test_a_limited_workspace_gets_fewer_channels_a_turn_and_its_quiet_channels_are_left_alone(monkeypatch):
    redis = _Redis()
    monkeypatch.setattr(read_limit, "get_redis_client", lambda: redis)
    unlimited = chat_intake._turn_size("T0OURS")
    read_limit.was_limited("T0OURS", read_limit.REPLIES, 30)
    channels, quiet = chat_intake._turn_size("T0OURS")
    assert unlimited[1] == 0
    assert channels < unlimited[0]
    assert quiet > 0


def test_claimed_channels_are_read_pending_first_then_never_read_then_longest_unread():
    # channel, last_ts, level, claim, threads, pending IS NULL, pending, updated IS NOT NULL, updated
    def row(channel, pending, updated):
        return (channel, None, "listen", None, {}, pending is None, pending, updated is not None, updated)

    chosen = [row("resumed", 0, 50), row("waiting longest", 10, 90), row("waiting", 20, 5),
              row("never read", None, None), row("read long ago", None, 30), row("read just now", None, 80)]
    shuffled = chosen[:]
    random.Random(7).shuffle(shuffled)
    assert [r[0] for r in chat_signals._as_chosen(shuffled)] == [r[0] for r in chosen]
