"""The "worth a look" tier: how many posts a channel gets, which failures get
them, and that a post can never turn into a notification.
"""

from datetime import datetime, timedelta, timezone

from services.detection.access import blind_message
from services.detection.attention import (
    BASE_WEEKLY_BUDGET,
    MAX_WEEKLY_BUDGET,
    strongest_first,
    weekly_budget,
    worth_a_look_message,
)
from services.detection.record import PatternRecord

_NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


def _candidate(name, pattern, hours_ago, **overrides):
    candidate = {
        "fire_id": name, "check_id": name, "name": name, "asset": "checkout", "pattern": pattern,
        "prediction": "checkout has fewer than 2 Running pods", "observed": "1",
        "assertion": {"expect": "lines", "op": "gte", "value": 2},
        "origin": {"kind": "person", "ref": "Riley"},
        "fired_at": _NOW - timedelta(hours=hours_ago), "hours_failing": float(hours_ago),
    }
    candidate.update(overrides)
    return candidate


def test_posts_that_pointed_at_nothing_shrink_the_budget_to_zero_and_no_further():
    assert weekly_budget(0, 0) == BASE_WEEKLY_BUDGET
    assert weekly_budget(0, BASE_WEEKLY_BUDGET) == 0
    assert weekly_budget(0, 50) == 0


def test_posts_that_pointed_at_real_incidents_grow_the_budget_up_to_the_cap():
    assert weekly_budget(2, 0) == BASE_WEEKLY_BUDGET + 2
    assert weekly_budget(50, 0) == MAX_WEEKLY_BUDGET


def test_a_kind_of_check_that_has_been_right_goes_before_an_older_unproven_failure():
    unproven_old = _candidate("unproven", "new-kind", hours_ago=30)
    trusted_recent = _candidate("trusted", "known-kind", hours_ago=3)
    records = {"known-kind": PatternRecord(hits=2, false_fires=0)}

    ordered = strongest_first([unproven_old, trusted_recent], records)

    assert [c["name"] for c in ordered] == ["trusted", "unproven"]


def test_among_equals_the_longest_failure_goes_first():
    ordered = strongest_first(
        [_candidate("recent", "k", hours_ago=3), _candidate("old", "k", hours_ago=30)], {},
    )
    assert [c["name"] for c in ordered] == ["old", "recent"]


def test_text_from_a_model_or_a_customer_system_cannot_mention_anyone():
    # The tier's promise is "no mention". A check name, a prediction or command
    # output carrying Slack mention syntax must arrive as plain characters.
    hostile = _candidate(
        "<!channel> urgent", "k", hours_ago=3,
        asset="<@U123ABC>", prediction="<!here> look", observed="<!everyone> & more",
    )
    message = worth_a_look_message(hostile, budget=3)
    assert "<!" not in message
    assert "<@" not in message

    blind = blind_message("gcp <!channel>", 2, "denied for <@U123ABC>")
    assert "<!" not in blind
    assert "<@" not in blind


def test_the_post_says_how_long_it_has_failed_and_why_aurora_watches():
    message = worth_a_look_message(_candidate("checkout running pods", "k", hours_ago=2.5), budget=3)
    assert "2h 30m" in message
    assert "Riley asked Aurora to watch this" in message
    assert "at least 2" in message
