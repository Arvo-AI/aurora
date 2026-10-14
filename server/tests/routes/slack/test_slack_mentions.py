"""Slack mentions need a user id. A display name never notifies."""

from routes.slack.slack_events_helpers import (
    format_asked_by,
    format_response_for_slack,
    format_slack_speaker,
    strip_bot_mentions,
)

ASKER = "U012ASKER1"
BOT = "U012BOT000"
OTHER = "U012OTHER1"


def test_speaker_label_is_a_real_mention():
    assert format_slack_speaker(ASKER, "Ada Lovelace") == f"<@{ASKER}> (Ada Lovelace)"


def test_speaker_without_id_stays_a_name():
    assert format_slack_speaker("", "Ada Lovelace") == "Ada Lovelace"


def test_bot_is_not_mentioned():
    assert format_slack_speaker(BOT, "Aurora", is_bot=True) == "Aurora"


def test_asked_by_carries_the_mention_even_without_a_display_name():
    assert format_asked_by(ASKER, None, "what failed?") == f"Asked by <@{ASKER}>: what failed?"


def test_asked_by_without_an_id_is_unchanged():
    assert format_asked_by(None, "Ada", "what failed?") == "what failed?"


def test_strip_keeps_other_people():
    text = f"<@{BOT}> ask <@{OTHER}> about the outage"
    assert strip_bot_mentions(text, [BOT]) == f"ask <@{OTHER}> about the outage"


def test_strip_drops_every_mention_when_the_bot_is_unknown():
    text = f"<@{BOT}> ask <@{OTHER}>"
    assert strip_bot_mentions(text, []) == "ask"


def test_reply_formatter_keeps_mentions():
    reply = f"*<@{ASKER}>* the pool is exhausted"
    formatted = format_response_for_slack(reply)
    assert f"<@{ASKER}>" in formatted
