"""How a command's output becomes pass, fail or error.

The rule that matters most: output a check cannot read is an error, never a
pass. A check that silently passes on garbage would report a broken system as
healthy for as long as it stays broken.
"""

import pytest

from services.detection import STATUS_ERROR, STATUS_FAIL, STATUS_PASS
from services.detection.assertion import evaluate, validation_error

ZERO_CRASHLOOPS = {"expect": "number", "op": "eq", "value": 0}


def test_number_within_the_healthy_condition_passes():
    assert evaluate(ZERO_CRASHLOOPS, "0\n").status == STATUS_PASS


def test_number_outside_the_healthy_condition_fails_and_keeps_the_value():
    verdict = evaluate(ZERO_CRASHLOOPS, "3\n")
    assert verdict.status == STATUS_FAIL
    assert verdict.observed == "3"


@pytest.mark.parametrize("output", [
    "",                                   # command printed nothing
    "error: You must be logged in",       # auth failure text instead of a count
    "0 pods",                             # a number with trailing text
    "nan",
    "inf",
    "1\n2",                               # two numbers: the filter is wrong
])
def test_output_that_is_not_one_finite_number_is_an_error_not_a_pass(output):
    # `lt 100` would pass on most garbage if unreadable output defaulted to 0.
    verdict = evaluate({"expect": "number", "op": "lt", "value": 100}, output)
    assert verdict.status == STATUS_ERROR


def test_text_conditions():
    healthy = {"expect": "text", "op": "contains", "value": "Running"}
    assert evaluate(healthy, "api-1  Running").status == STATUS_PASS
    assert evaluate(healthy, "api-1  CrashLoopBackOff").status == STATUS_FAIL

    nothing_stuck = {"expect": "text", "op": "empty"}
    assert evaluate(nothing_stuck, "  \n").status == STATUS_PASS
    assert evaluate(nothing_stuck, "job-42 Pending").status == STATUS_FAIL


def test_an_unusable_assertion_is_an_error_at_run_time():
    # A row edited behind the validator must not turn into a pass.
    assert evaluate({"expect": "number", "op": "contains", "value": 1}, "1").status == STATUS_ERROR
    assert evaluate({"expect": "regex", "op": "eq", "value": "x"}, "x").status == STATUS_ERROR


@pytest.mark.parametrize("assertion", [
    {"expect": "number", "op": "lt", "value": "many"},
    {"expect": "number", "op": "lt", "value": True},
    {"expect": "number", "op": "lt", "value": float("nan")},
    {"expect": "text", "op": "contains", "value": ""},   # '' is in every string
    {"expect": "text", "op": "contains"},
    "count < 5",
])
def test_assertions_that_could_never_judge_anything_are_refused(assertion):
    assert validation_error(assertion)


def test_lines_counts_non_empty_output_lines():
    none_failed = {"expect": "lines", "op": "eq", "value": 0}
    assert evaluate(none_failed, "").status == STATUS_PASS
    assert evaluate(none_failed, "\n  \n").status == STATUS_PASS
    verdict = evaluate(none_failed, "job-1 Failed\njob-2 Failed\n")
    assert verdict.status == STATUS_FAIL


def test_a_stored_assertion_no_validator_saw_becomes_an_error_not_a_crash():
    # A crash here happens after the command ran and before the run is recorded,
    # so the check would look like it never ran.
    assert evaluate({"expect": "number", "op": [], "value": 1}, "1").status == STATUS_ERROR
    assert evaluate({"expect": "number", "op": "lt", "value": 10 ** 400}, "1").status == STATUS_ERROR
