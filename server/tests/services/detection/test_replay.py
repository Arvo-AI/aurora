"""Replay: a check written for an incident is tried against that incident.

The two ways a model-written check goes wrong are covered by the two halves of
a replay: it measures the wrong thing (stays healthy over its own incident), or
it is noise (fails on days when nothing happened).
"""

from datetime import datetime, timedelta, timezone

from services.detection import STATUS_ERROR, STATUS_FAIL, STATUS_PASS, window
from services.detection.assertion import Verdict
from services.detection.replay import (
    incident_id_from,
    refusal,
    replay_end,
    replay_origin,
    still_catches,
)
from services.detection.store import normalize_definition

_INCIDENT_AT = datetime(2026, 11, 10, 14, 0, tzinfo=timezone.utc)
_END = _INCIDENT_AT + timedelta(minutes=15)
_QUIET = [_END - timedelta(days=d) for d in (1, 2, 3)]
_CHECK = {
    "provider": "gcp",
    "command": "logging read 'severity>=ERROR AND timestamp>={from} AND timestamp<={to}' --format=json | jq length",
    "assertion": {"expect": "number", "op": "lt", "value": 50},
    "window_seconds": 900,
}


def _runner(by_end):
    """A stand-in executor: the verdict for each span end; healthy when not listed."""
    seen = []

    def run(check, user_id, check_id, at=None):
        seen.append(at)
        status = by_end.get(at, STATUS_PASS)
        return Verdict(status, "" if status == STATUS_ERROR else "7")

    run.seen = seen
    return run


def test_a_check_that_fails_over_its_incident_and_is_quiet_before_is_admitted():
    result = replay_origin(_CHECK, "u", _END, _QUIET, run=_runner({_END: STATUS_FAIL}))
    assert result.caught is True
    assert (result.quiet_runs, result.quiet_fails) == (3, 0)
    assert refusal(result) == ""


def test_a_check_that_stays_healthy_over_its_own_incident_is_refused():
    result = replay_origin(_CHECK, "u", _END, _QUIET, run=_runner({}))
    assert result.caught is False
    assert "would not have caught it" in refusal(result)


def test_a_check_that_also_fails_on_quiet_days_is_refused_as_noise():
    always_failing = _runner({_END: STATUS_FAIL, _QUIET[0]: STATUS_FAIL, _QUIET[2]: STATUS_FAIL})
    result = replay_origin(_CHECK, "u", _END, _QUIET, run=always_failing)
    assert result.quiet_fails == 2
    assert "quiet history" in refusal(result)


def test_a_span_the_source_no_longer_holds_is_not_held_against_the_check():
    # Retention ran out: the check is admitted silent, with nothing claimed for it.
    result = replay_origin(_CHECK, "u", _END, _QUIET, run=_runner({_END: STATUS_ERROR}))
    assert result.caught is None
    assert refusal(result) == ""


def test_quiet_days_that_cannot_be_read_are_not_counted_as_quiet():
    result = replay_origin(
        _CHECK, "u", _END, _QUIET, run=_runner({_END: STATUS_FAIL, _QUIET[1]: STATUS_ERROR}),
    )
    assert (result.quiet_runs, result.quiet_fails) == (2, 0)


def test_a_check_that_reads_the_present_is_never_replayed():
    run = _runner({})
    point_in_time = {"provider": "gcp", "command": "compute instances list", "window_seconds": 0,
                     "assertion": {"expect": "lines", "op": "gte", "value": 1}}
    from_only = dict(_CHECK, command="logging read 'timestamp>={from}' --format=json | jq length")

    assert replay_origin(point_in_time, "u", _END, _QUIET, run=run).caught is None
    # Without an end, "last week" would also read everything since.
    assert replay_origin(from_only, "u", _END, _QUIET, run=run).caught is None
    assert run.seen == []


def test_the_replayed_span_never_reaches_into_the_future():
    now = _INCIDENT_AT + timedelta(minutes=5)
    assert replay_end(_INCIDENT_AT, 900, now=now) == now
    assert replay_end(_INCIDENT_AT, 900, now=now + timedelta(days=1)) == _END


def test_the_incident_is_found_inside_a_free_text_reference():
    assert incident_id_from("INC 3F2A9C1E-5b7d-4e8a-9c21-7d4e5f6a8b90 (checkout)") == "3f2a9c1e-5b7d-4e8a-9c21-7d4e5f6a8b90"
    assert incident_id_from("INC 12") is None
    assert incident_id_from("") is None


def test_a_fire_drill_tells_a_check_that_now_misses_from_one_that_cannot_be_read():
    target = dict(_CHECK, id="c1", created_by="u", replay={"replayed_to": _END.isoformat()})
    assert still_catches(target, run=_runner({_END: STATUS_FAIL})) is True
    assert still_catches(target, run=_runner({})) is False
    assert still_catches(target, run=_runner({_END: STATUS_ERROR})) is None


def test_a_drill_over_a_span_with_no_data_left_does_not_demote():
    # Past the source's retention the span comes back empty. A line count reads
    # that as zero lines and healthy, which is not the check having stopped working.
    target = dict(_CHECK, id="c1", created_by="u", replay={"replayed_to": _END.isoformat()},
                  assertion={"expect": "lines", "op": "lt", "value": 50})

    def aged_out(check, user_id, check_id, at=None):
        return Verdict(STATUS_PASS, "")

    assert still_catches(target, run=aged_out) is None


def test_one_quiet_day_is_not_enough_to_be_credited_with_the_incident():
    one_readable = _runner({_END: STATUS_FAIL, _QUIET[1]: STATUS_ERROR, _QUIET[2]: STATUS_ERROR})
    result = replay_origin(_CHECK, "u", _END, _QUIET, run=one_readable)
    assert result.caught is None
    assert refusal(result) == ""


def test_one_quiet_day_that_fails_still_refuses_the_check():
    noisy = _runner({_END: STATUS_FAIL, _QUIET[0]: STATUS_FAIL,
                     _QUIET[1]: STATUS_ERROR, _QUIET[2]: STATUS_ERROR})
    assert "quiet history" in refusal(replay_origin(_CHECK, "u", _END, _QUIET, run=noisy))


def test_a_replay_stops_running_commands_once_its_time_is_used_up():
    run = _runner({_END: STATUS_FAIL})
    ticks = iter([0, 40, 80, 120, 160])   # each command took 40 seconds
    result = replay_origin(_CHECK, "u", _END, _QUIET, run=run, clock=lambda: next(ticks))
    assert len(run.seen) == 3              # the incident and two quiet days, not the third
    assert result.caught is True


def test_the_span_can_be_written_as_epoch_seconds_or_milliseconds():
    seconds = window.render("logs --start {from_epoch} --end {to_epoch}", 900, _END)
    millis = window.render("aws logs filter-log-events --start-time {from_epoch_ms} --end-time {to_epoch_ms}", 900, _END)
    start, end = int((_END - timedelta(minutes=15)).timestamp()), int(_END.timestamp())
    assert seconds == "logs --start %d --end %d" % (start, end)
    assert millis == "aws logs filter-log-events --start-time %d000 --end-time %d000" % (start, end)
    assert window.is_replayable("x {from_epoch_ms} {to_epoch_ms}", 900)
    assert not window.is_replayable("x {from_epoch_ms}", 900)


def test_a_lagging_source_is_read_up_to_the_lag_not_up_to_now():
    now = datetime(2026, 11, 10, 14, 15, tzinfo=timezone.utc)
    end = window.span_end(120, now=now)
    assert window.render("logs {from} {to}", 600, end) == "logs 2026-11-10T14:03:00Z 2026-11-10T14:13:00Z"


def test_the_span_is_filled_in_with_plain_utc_timestamps():
    rendered = window.render("logs --from {from} --to {to}", 900, _END)
    assert rendered == "logs --from 2026-11-10T14:00:00Z --to 2026-11-10T14:15:00Z"
    assert window.render("compute instances list", 0, _END) == "compute instances list"


def _saved(**overrides):
    raw = {
        "name": "api errors", "prediction": "api logs a burst of errors", "asset": "api",
        "pattern": "log-error-burst", "provider": "gcp", "interval_seconds": 300,
        "command": _CHECK["command"], "assertion": _CHECK["assertion"], "window_seconds": 900,
    }
    raw.update(overrides)
    return normalize_definition(raw)


def test_a_windowed_check_is_validated_as_the_command_that_will_actually_run():
    assert _saved()[1] == ""
    assert "window_seconds is required" in _saved(window_seconds=0)[1]
    assert "{from}" in _saved(command="logging read 'severity>=ERROR' | jq length")[1]
    assert "between" in _saved(window_seconds=5)[1]
    # One minute read every fifteen leaves fourteen that no run ever looks at.
    assert "shorter than interval_seconds" in _saved(window_seconds=60, interval_seconds=900)[1]
    assert "lag_seconds" in _saved(lag_seconds=7200)[1]
    assert _saved(lag_seconds=120)[0]["lag_seconds"] == 120
    # A write hidden behind the placeholders is still a write once they are filled in.
    assert "not read-only" in _saved(command="logging sinks delete audit --since {from}")[1]


def test_a_replay_with_no_readable_quiet_day_earns_nothing():
    # Failing over the incident alone proves little: a check that always fails
    # does that too. With no quiet day to compare, it is admitted without credit.
    every_quiet_day_unreadable = _runner({_END: STATUS_FAIL, **{q: STATUS_ERROR for q in _QUIET}})
    assert replay_origin(_CHECK, "u", _END, _QUIET, run=every_quiet_day_unreadable).caught is None
    assert replay_origin(_CHECK, "u", _END, [], run=_runner({_END: STATUS_FAIL})).caught is None


def test_a_check_that_missed_its_incident_is_refused_even_when_quiet_days_are_unreadable():
    # Healthy over the incident is a miss whatever the quiet days say, or fail to say.
    missed = _runner({_QUIET[1]: STATUS_ERROR, _QUIET[2]: STATUS_ERROR})
    result = replay_origin(_CHECK, "u", _END, _QUIET, run=missed)
    assert result.caught is False
    assert refusal(result)
