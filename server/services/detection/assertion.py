"""Turn a command's output into pass, fail or error, with no model.

An assertion states the healthy condition: the check passes while
``output <op> value`` holds. Shaping the output (counting, picking a JSON
field) belongs in the command itself, through the text filters the read-only
classifier already allows (``grep -c``, ``jq``, ``wc -l``), so no pattern
supplied by a model is ever evaluated here.

Output that cannot be read as the assertion expects is an error, never a pass:
a check that cannot see must not look healthy.
"""

import math
from dataclasses import dataclass
from typing import Any, Optional

from services.detection import STATUS_ERROR, STATUS_FAIL, STATUS_PASS

EXPECT_NUMBER = "number"
EXPECT_TEXT = "text"
# The count of non-empty output lines. For executors with no shell, where the
# command cannot pipe into `wc -l` (on-prem kubectl).
EXPECT_LINES = "lines"
_COUNTED = (EXPECT_NUMBER, EXPECT_LINES)

_NUMBER_OPS = {
    "lt": lambda seen, want: seen < want,
    "lte": lambda seen, want: seen <= want,
    "gt": lambda seen, want: seen > want,
    "gte": lambda seen, want: seen >= want,
    "eq": lambda seen, want: seen == want,
    "ne": lambda seen, want: seen != want,
}
_TEXT_OPS = {
    "contains": lambda seen, want: want in seen,
    "not_contains": lambda seen, want: want not in seen,
    "eq": lambda seen, want: seen == want,
    "ne": lambda seen, want: seen != want,
    "empty": lambda seen, _want: seen == "",
    "not_empty": lambda seen, _want: seen != "",
}
_VALUELESS_OPS = ("empty", "not_empty")

OBSERVED_MAX_CHARS = 500


@dataclass(frozen=True)
class Verdict:
    status: str
    observed: str = ""
    detail: str = ""
    # False when the command could not be executed at all (no access, blocked,
    # timed out). An error with ran=True reached the system and could not read
    # what came back, which says nothing about Aurora's access.
    ran: bool = True


def _as_number(value: Any) -> Optional[float]:
    """A finite float, or None. Booleans are not numbers here."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
    elif not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        # OverflowError: an integer too large for a float, e.g. 10**400.
        return None
    return number if math.isfinite(number) else None


def validation_error(assertion: Any) -> str:
    """Why *assertion* cannot be used, or '' when it can."""
    if not isinstance(assertion, dict):
        return "assertion must be an object with expect, op and value"
    expect = assertion.get("expect")
    op = assertion.get("op")
    if not isinstance(op, str):
        # An unhashable op (a list, a dict) would raise on the lookups below.
        return "op must be a string"
    if expect in _COUNTED:
        if op not in _NUMBER_OPS:
            return "op for a number must be one of: " + ", ".join(sorted(_NUMBER_OPS))
        if _as_number(assertion.get("value")) is None:
            return "value must be a finite number"
        return ""
    if expect == EXPECT_TEXT:
        if op not in _TEXT_OPS:
            return "op for text must be one of: " + ", ".join(sorted(_TEXT_OPS))
        if op in _VALUELESS_OPS:
            return ""
        value = assertion.get("value")
        if not isinstance(value, str) or not value:
            return "value must be a non-empty string"
        return ""
    return "expect must be 'number', 'lines' or 'text'"


def evaluate(assertion: dict, output: str) -> Verdict:
    """Judge *output* against *assertion*."""
    problem = validation_error(assertion)
    if problem:
        return Verdict(STATUS_ERROR, detail=problem)

    text = (output or "").strip()
    observed = text[:OBSERVED_MAX_CHARS]
    op = assertion["op"]

    if assertion["expect"] == EXPECT_LINES:
        seen = float(sum(1 for line in text.splitlines() if line.strip()))
        healthy = _NUMBER_OPS[op](seen, _as_number(assertion["value"]))
    elif assertion["expect"] == EXPECT_NUMBER:
        if not text:
            # The command ran and printed nothing where a number was expected.
            # That is what "gone" looks like: Kubernetes omits readyReplicas at
            # zero replicas. Calling it an error would hide the full outage the
            # check exists for, and calling it zero could pass `lt` checks, so
            # it is a failure whatever the condition.
            return Verdict(STATUS_FAIL, "", "the command printed nothing")
        seen = _as_number(text)
        if seen is None:
            return Verdict(STATUS_ERROR, observed, "output is not a single number")
        healthy = _NUMBER_OPS[op](seen, _as_number(assertion["value"]))
    else:
        healthy = _TEXT_OPS[op](text, assertion.get("value") or "")

    return Verdict(STATUS_PASS if healthy else STATUS_FAIL, observed)
