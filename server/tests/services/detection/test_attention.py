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


class _Slack:
    def __init__(self, outcome):
        self.outcome = outcome

    def send_message(self, channel, text, thread_ts=None, blocks=None):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def test_only_a_post_slack_refused_may_be_tried_again():
    # A request that died in transit may already have been posted. Trying again
    # could post it twice, in the one tier that promises to be quiet.
    from connectors.slack_connector.client import SlackAPIError
    from services.detection import runner

    assert runner._send(_Slack({"ok": True, "ts": "1.2"}), "C1", "x") == ("1.2", False)
    assert runner._send(_Slack(SlackAPIError("channel_not_found")), "C1", "x") == (None, True)
    assert runner._send(_Slack(ConnectionError("reset by peer")), "C1", "x") == (None, False)


class _SlackUser:
    def __init__(self, tz_offset):
        self.tz_offset = tz_offset

    def _make_request(self, method, endpoint, params):
        if isinstance(self.tz_offset, Exception):
            raise self.tz_offset
        return {"ok": True, "user": {"tz_offset": self.tz_offset}}


def test_a_post_waits_for_a_weekday_daytime_where_the_team_is():
    from datetime import datetime, timezone

    from services.detection.attention import working_hours_now

    tuesday_0700_utc = datetime(2026, 11, 24, 7, 0, tzinfo=timezone.utc)
    assert not working_hours_now(_SlackUser(0), "U1", now=tuesday_0700_utc)          # 07:00 in London
    assert working_hours_now(_SlackUser(3600), "U1", now=tuesday_0700_utc)           # 08:00 in Paris
    assert not working_hours_now(_SlackUser(-5 * 3600), "U1", now=tuesday_0700_utc)  # 02:00 in Toronto
    saturday_noon_utc = datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc)
    assert not working_hours_now(_SlackUser(0), "U1", now=saturday_noon_utc)


def test_an_unknown_timezone_falls_back_to_utc_and_still_keeps_the_weekend_clear():
    from datetime import datetime, timezone

    from services.detection.attention import working_hours_now

    tuesday_noon_utc = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
    saturday_noon_utc = datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc)
    unknown = ((_SlackUser(ConnectionError("slack down")), "U1"), (_SlackUser(None), "U1"), (_SlackUser(0), None))
    for client, installer in unknown:
        # It still goes out, so an install with no timezone is not silent for good...
        assert working_hours_now(client, installer, now=tuesday_noon_utc)
        # ...and not on a Saturday.
        assert not working_hours_now(client, installer, now=saturday_noon_utc)
