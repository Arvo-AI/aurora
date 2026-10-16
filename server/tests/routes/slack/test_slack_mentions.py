"""Slack mentions need a user id. A display name never notifies."""

from routes.slack.slack_events_helpers import (
    format_asked_by,
    format_response_for_slack,
    format_slack_speaker,
    mention_bot_ids,
    slack_session_title,
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


def test_labeled_bot_mention_is_stripped_and_people_stay():
    text = f"<@{BOT}|Aurora> ask <@{OTHER}|Ada> about it"
    assert strip_bot_mentions(text, [BOT]) == f"ask <@{OTHER}|Ada> about it"


def test_bare_labeled_mention_counts_as_no_question():
    assert strip_bot_mentions(f"<@{BOT}|Aurora>", [BOT]) == ""


def test_unclosed_label_does_not_swallow_the_next_mention():
    text = f"<@{BOT}|Ada ask <@{OTHER}>"
    assert strip_bot_mentions(text, [BOT]) == text


def test_labeled_mentions_dropped_when_the_bot_is_unknown():
    text = f"<@{BOT}|Aurora> ask <@{OTHER}|Ada>"
    assert strip_bot_mentions(text, []) == "ask"


def test_missing_authorizations_keep_the_stored_bot_id():
    assert mention_bot_ids(None, BOT) == [BOT]
    assert mention_bot_ids([], BOT) == [BOT]


def test_authorizations_win_over_the_stored_bot_id():
    auths = [{"user_id": BOT, "is_bot": True}, {"user_id": ASKER, "is_bot": False}]
    assert mention_bot_ids(auths, "USTORED999") == [BOT]


def test_session_title_is_the_question():
    message = format_asked_by(ASKER, "Ada Lovelace", "what failed in the production cluster?")
    assert slack_session_title(message) == "Slack: what failed in the production cluster?"


def test_session_title_truncates_the_question_not_the_prefix():
    question = "q" * 60
    message = format_asked_by(ASKER, "Ada", question)
    assert slack_session_title(message) == "Slack: " + ("q" * 50) + "..."


def test_reply_formatter_keeps_mentions():
    reply = f"*<@{ASKER}>* the pool is exhausted"
    formatted = format_response_for_slack(reply)
    assert f"<@{ASKER}>" in formatted
