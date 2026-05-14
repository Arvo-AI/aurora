"""Unit tests for the verdict validator.

The validator is the trust boundary between the LLM and the customer-
visible review. Every drop / downgrade / dedup / cap path is pinned
here so a regression can't silently let a hallucinated finding through
to a PR comment.
"""

from __future__ import annotations

from typing import Any

import pytest

from services.change_intercept.verdict_validator import (
    MAX_INLINE_COMMENTS,
    DroppedFinding,
    ValidatedFinding,
    ValidationResult,
    validate,
)


# Two small reusable diff fixtures.


_DIFF_FOO_PY = """diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -10,2 +10,5 @@
 ctx
-old
+a
+b
+c
 ctx
"""

_DIFF_WIDE = """diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -10,2 +10,10 @@
 ctx
-old
+l10
+l11
+l12
+l13
+l14
+l15
+l16
+l17
+l18
 ctx
"""


def _good_finding(**overrides: Any) -> dict[str, Any]:
    """A baseline finding the validator should accept."""
    base = {
        "severity": "HIGH",
        "confidence": "HIGH",
        "category": "missing_timeout",
        "file_path": "foo.py",
        "start_line": 11,
        "end_line": None,
        "title": "HTTP call without timeout",
        "rationale": "requests.get on line 11 has no timeout. [diff]",
        "cited_tool_calls": [],
    }
    base.update(overrides)
    return base


def _raw_output(verdict: str = "request_changes", findings: list[Any] | None = None) -> dict[str, Any]:
    return {
        "verdict": verdict,
        "summary": "test",
        "intent_alignment": "matches",
        "intent_notes": None,
        "findings": findings or [],
    }


# ─── Top-level shape ─────────────────────────────────────────────────


def test_non_dict_input_returns_safe_approve() -> None:
    r = validate("not a dict", _DIFF_FOO_PY)  # type: ignore[arg-type]
    assert isinstance(r, ValidationResult)
    assert r.verdict == "approve"
    assert r.findings == []
    assert r.drop_log  # records why it bailed


def test_missing_verdict_returns_safe_approve() -> None:
    r = validate({"findings": []}, _DIFF_FOO_PY)
    assert r.verdict == "approve" and r.findings == []


def test_invalid_verdict_returns_safe_approve() -> None:
    r = validate({"verdict": "block", "summary": "x", "findings": []}, _DIFF_FOO_PY)
    assert r.verdict == "approve"


def test_empty_summary_renders_placeholder() -> None:
    r = validate(_raw_output(), _DIFF_FOO_PY)
    # Default summary should be non-empty even when no findings exist.
    assert r.summary


# ─── Happy path ─────────────────────────────────────────────────────


def test_high_high_finding_survives_and_posts_inline() -> None:
    r = validate(_raw_output(findings=[_good_finding()]), _DIFF_FOO_PY)
    assert r.verdict == "request_changes"
    assert len(r.findings) == 1
    assert r.findings[0].will_post_inline


def test_medium_finding_survives_but_does_not_post_inline() -> None:
    r = validate(
        _raw_output(findings=[_good_finding(severity="MEDIUM")]),
        _DIFF_FOO_PY,
    )
    assert len(r.findings) == 1
    assert not r.findings[0].will_post_inline
    # No HIGH+HIGH survived → downgrade to approve.
    assert r.verdict == "approve" and r.downgraded_to_approve


def test_low_finding_appears_in_body_only() -> None:
    r = validate(
        _raw_output(verdict="approve", findings=[_good_finding(severity="LOW")]),
        _DIFF_FOO_PY,
    )
    assert len(r.findings) == 1
    assert not r.findings[0].will_post_inline


def test_intent_alignment_is_preserved_when_set() -> None:
    raw = _raw_output(findings=[_good_finding()])
    raw["intent_alignment"] = "mismatch"
    raw["intent_notes"] = "diff adds new dep but body says refactor"
    r = validate(raw, _DIFF_FOO_PY)
    assert r.intent_alignment == "mismatch"
    assert r.intent_notes == "diff adds new dep but body says refactor"


def test_intent_notes_dropped_when_alignment_matches() -> None:
    raw = _raw_output(findings=[_good_finding()])
    raw["intent_alignment"] = "matches"
    raw["intent_notes"] = "should not appear"
    r = validate(raw, _DIFF_FOO_PY)
    assert r.intent_notes is None


def test_invalid_intent_alignment_is_nulled() -> None:
    raw = _raw_output(findings=[_good_finding()])
    raw["intent_alignment"] = "bogus_value"
    r = validate(raw, _DIFF_FOO_PY)
    assert r.intent_alignment is None


# ─── Per-finding drop paths ─────────────────────────────────────────


def test_unknown_category_drops_finding() -> None:
    raw = _raw_output(findings=[_good_finding(category="not_in_taxonomy")])
    r = validate(raw, _DIFF_FOO_PY)
    assert r.findings == []
    assert any(d.reason == "unknown_category" for d in r.dropped)


def test_bad_severity_drops_finding() -> None:
    raw = _raw_output(findings=[_good_finding(severity="CRITICAL")])
    r = validate(raw, _DIFF_FOO_PY)
    assert r.findings == []
    assert any(d.reason == "bad_severity" for d in r.dropped)


def test_bad_confidence_defaults_to_medium() -> None:
    raw = _raw_output(findings=[_good_finding(confidence="UNKNOWN")])
    r = validate(raw, _DIFF_FOO_PY)
    # Confidence is not load-bearing for verdict; we default to MEDIUM.
    assert len(r.findings) == 1
    assert r.findings[0].confidence == "MEDIUM"
    # MEDIUM confidence -> not inline -> request_changes downgraded.
    assert r.verdict == "approve"


def test_missing_path_drops_finding() -> None:
    raw = _raw_output(findings=[_good_finding(file_path="")])
    r = validate(raw, _DIFF_FOO_PY)
    assert any(d.reason == "missing_path" for d in r.dropped)


def test_missing_line_drops_finding() -> None:
    raw = _raw_output(findings=[_good_finding(start_line=0)])
    r = validate(raw, _DIFF_FOO_PY)
    assert any(d.reason == "missing_line" for d in r.dropped)


def test_line_outside_diff_drops_finding() -> None:
    raw = _raw_output(findings=[_good_finding(start_line=9999)])
    r = validate(raw, _DIFF_FOO_PY)
    assert any(d.reason == "unanchored_line" for d in r.dropped)
    assert r.findings == []


def test_context_line_anchor_downgrades_confidence_keeps_finding() -> None:
    # Line 10 is the first context line in the hunk (new_start=10), not
    # an added line. The lax-anchor path keeps the finding but downgrades
    # confidence so it doesn't qualify for inline posting.
    raw = _raw_output(findings=[_good_finding(start_line=10)])
    r = validate(raw, _DIFF_FOO_PY)
    assert len(r.findings) == 1
    assert r.findings[0].confidence != "HIGH"
    assert not r.findings[0].will_post_inline


def test_hand_wavy_rationale_drops_finding() -> None:
    raw = _raw_output(
        findings=[_good_finding(rationale="i feel this could be unsafe")]
    )
    r = validate(raw, _DIFF_FOO_PY)
    assert any(d.reason == "no_diff_anchor" for d in r.dropped)


def test_fake_tool_call_alone_is_insufficient_in_single_call_mode() -> None:
    """Phase 1a is single-call (no agentic toolset); any tool-call list
    the LLM produces is fabricated by definition. Mechanical [diff]
    anchor is required for EVERY finding — the cited_tool_calls field
    is persisted for forward-compat but never bypasses the gate."""
    raw = _raw_output(
        findings=[
            _good_finding(
                rationale="this looks bad",
                cited_tool_calls=[
                    {"tool": "incident_feedback", "call_id": "x", "summary": "..."}
                ],
            )
        ]
    )
    r = validate(raw, _DIFF_FOO_PY)
    assert r.findings == []
    assert any(d.reason == "no_diff_anchor" for d in r.dropped)


def test_finding_with_diff_anchor_and_tool_calls_passes() -> None:
    """When the rationale carries the [diff] mechanical anchor, the
    finding survives; cited_tool_calls is preserved on the row but
    doesn't change the validation outcome."""
    raw = _raw_output(
        findings=[
            _good_finding(
                rationale="requests.get on line 11 has no timeout [diff]",
                cited_tool_calls=[
                    {"tool": "incident_feedback", "call_id": "x", "summary": "..."}
                ],
            )
        ]
    )
    r = validate(raw, _DIFF_FOO_PY)
    assert len(r.findings) == 1
    assert r.findings[0].cited_tool_calls == [
        {"tool": "incident_feedback", "call_id": "x", "summary": "..."}
    ]


# ─── Dedup ──────────────────────────────────────────────────────────


def test_dedup_keeps_higher_severity_at_same_location() -> None:
    raw = _raw_output(
        findings=[
            _good_finding(severity="MEDIUM", title="A"),
            _good_finding(severity="HIGH", title="B"),
        ]
    )
    r = validate(raw, _DIFF_FOO_PY)
    assert len(r.findings) == 1
    assert r.findings[0].title == "B"
    assert any(d.reason == "duplicate" for d in r.dropped)


def test_dedup_keeps_higher_confidence_when_severity_ties() -> None:
    raw = _raw_output(
        findings=[
            _good_finding(severity="HIGH", confidence="MEDIUM", title="A"),
            _good_finding(severity="HIGH", confidence="HIGH", title="B"),
        ]
    )
    r = validate(raw, _DIFF_FOO_PY)
    assert len(r.findings) == 1
    assert r.findings[0].title == "B"


def test_dedup_does_not_collapse_different_categories() -> None:
    raw = _raw_output(
        findings=[
            _good_finding(category="missing_timeout"),
            _good_finding(category="unbounded_retry"),
        ]
    )
    r = validate(raw, _DIFF_FOO_PY)
    assert len(r.findings) == 2


# ─── Inline cap ─────────────────────────────────────────────────────


def test_inline_cap_max_three() -> None:
    raw = _raw_output(
        findings=[
            _good_finding(start_line=11 + i, title=f"T{i}") for i in range(5)
        ]
    )
    r = validate(raw, _DIFF_WIDE)
    inline = [f for f in r.findings if f.will_post_inline]
    assert len(inline) == MAX_INLINE_COMMENTS == 3


def test_inline_cap_prefers_higher_severity() -> None:
    raw = _raw_output(
        findings=[
            _good_finding(
                start_line=11 + i,
                severity="MEDIUM" if i < 3 else "HIGH",
                title=f"T{i}",
            )
            for i in range(5)
        ]
    )
    r = validate(raw, _DIFF_WIDE)
    inline = [f for f in r.findings if f.will_post_inline]
    # MEDIUM never qualifies for inline → only the two HIGH+HIGH go inline.
    assert all(f.severity == "HIGH" for f in inline)
    assert len(inline) == 2


# ─── Verdict reconciliation ─────────────────────────────────────────


def test_request_changes_without_high_high_downgrades_to_approve() -> None:
    raw = _raw_output(findings=[_good_finding(severity="MEDIUM")])
    r = validate(raw, _DIFF_FOO_PY)
    assert r.verdict == "approve" and r.downgraded_to_approve


def test_approve_with_high_high_remains_approve() -> None:
    # Sometimes the investigator wants approve even with a HIGH finding
    # (e.g. it's a noted risk but the engineer's reply addressed it).
    raw = _raw_output(verdict="approve", findings=[_good_finding()])
    r = validate(raw, _DIFF_FOO_PY)
    assert r.verdict == "approve"
    assert not r.downgraded_to_approve
