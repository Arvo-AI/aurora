"""Unit tests for the vendor-neutral review-body + inline-comment renderers.

The review-poster is the last hop before the customer sees Aurora's
verdict, so the body templates are an externalised contract. Every
section ordering / disclaimer / heading is pinned here.
"""

from __future__ import annotations

from services.change_intercept.pr_review_poster import (
    RenderedReview,
    render_review,
)
from services.change_intercept.verdict_validator import (
    ValidatedFinding,
    ValidationResult,
)


def _result(
    verdict: str = "request_changes",
    findings: list[ValidatedFinding] | None = None,
    intent_alignment: str | None = "matches",
    intent_notes: str | None = None,
    downgraded: bool = False,
) -> ValidationResult:
    return ValidationResult(
        verdict=verdict,
        summary="Aurora reviewed this PR.",
        intent_alignment=intent_alignment,
        intent_notes=intent_notes,
        findings=findings or [],
        downgraded_to_approve=downgraded,
    )


def _finding(
    severity: str = "HIGH",
    confidence: str = "HIGH",
    category: str = "missing_timeout",
    file_path: str = "server/foo.py",
    start_line: int = 42,
    title: str = "HTTP call without timeout",
    will_post_inline: bool = True,
    cited_tool_calls: list[dict] | None = None,
) -> ValidatedFinding:
    return ValidatedFinding(
        severity=severity,
        confidence=confidence,
        category=category,
        file_path=file_path,
        start_line=start_line,
        end_line=None,
        title=title,
        rationale=f"{title} — concrete production failure mode here.",
        cited_tool_calls=cited_tool_calls or [],
        will_post_inline=will_post_inline,
    )


# ─── Verdict event mapping ──────────────────────────────────────────


def test_request_changes_maps_to_request_changes_event() -> None:
    r = render_review(_result(verdict="request_changes"))
    assert r.verdict_event == "REQUEST_CHANGES"


def test_approve_maps_to_approve_event() -> None:
    r = render_review(_result(verdict="approve"))
    assert r.verdict_event == "APPROVE"


# ─── Top-level body structure ───────────────────────────────────────


def test_approve_body_has_approval_header() -> None:
    r = render_review(_result(verdict="approve"))
    assert "Aurora reviewed this PR" in r.body
    assert "flagged production-deployment risk" not in r.body


def test_request_changes_body_has_flag_header() -> None:
    r = render_review(_result(verdict="request_changes"))
    assert "Aurora flagged production-deployment risk" in r.body


def test_body_always_includes_advisory_footer() -> None:
    r = render_review(_result(verdict="approve"))
    assert "advisory reviewer" in r.body
    assert "Dismiss this review" in r.body


def test_body_includes_summary() -> None:
    r = render_review(_result())
    assert "Aurora reviewed this PR." in r.body


# ─── Findings sections ──────────────────────────────────────────────


def test_high_finding_renders_in_risks_section_with_severity_tag() -> None:
    r = render_review(_result(findings=[_finding()]))
    assert "### Risks identified" in r.body
    assert "[HIGH]" in r.body
    assert "Missing timeout" in r.body  # label, not slug
    assert "`server/foo.py:42`" in r.body


def test_medium_finding_renders_in_risks_section() -> None:
    r = render_review(
        _result(
            verdict="approve",
            findings=[_finding(severity="MEDIUM", will_post_inline=False)],
        )
    )
    assert "### Risks identified" in r.body
    assert "[MEDIUM]" in r.body


def test_low_finding_renders_in_other_notes_only() -> None:
    r = render_review(
        _result(
            verdict="approve",
            findings=[_finding(severity="LOW", will_post_inline=False)],
        )
    )
    assert "### Other notes" in r.body
    assert "[LOW]" in r.body
    # Should NOT appear under Risks (Risks section omitted entirely).
    assert "### Risks identified" not in r.body


def test_high_medium_low_render_in_distinct_sections() -> None:
    findings = [
        _finding(severity="HIGH", start_line=11),
        _finding(severity="MEDIUM", start_line=12, will_post_inline=False),
        _finding(severity="LOW", start_line=13, will_post_inline=False),
    ]
    r = render_review(_result(findings=findings))
    risks_idx = r.body.index("### Risks identified")
    other_idx = r.body.index("### Other notes")
    # Risks before Other notes.
    assert risks_idx < other_idx


def test_no_findings_yields_no_risks_or_other_sections() -> None:
    r = render_review(_result(verdict="approve"))
    assert "### Risks identified" not in r.body
    assert "### Other notes" not in r.body


# ─── Intent alignment ───────────────────────────────────────────────


def test_intent_mismatch_renders_intent_check_line() -> None:
    r = render_review(
        _result(
            intent_alignment="mismatch",
            intent_notes="PR body says refactor but adds new dep",
        )
    )
    assert "**Intent check:**" in r.body
    assert "PR body says refactor" in r.body


def test_intent_matches_omits_intent_check_line() -> None:
    r = render_review(_result(intent_alignment="matches"))
    assert "Intent check" not in r.body


def test_intent_partial_with_no_notes_omits_intent_check_line() -> None:
    # The validator already nulls intent_notes when alignment=matches,
    # but for partial/mismatch with empty notes we still want to omit
    # the bullet rather than render an empty one.
    r = render_review(_result(intent_alignment="partial", intent_notes=None))
    assert "Intent check" not in r.body


# ─── Downgrade notice ──────────────────────────────────────────────


def test_downgrade_to_approve_renders_calibration_note() -> None:
    r = render_review(
        _result(verdict="approve", downgraded=True)
    )
    assert "investigator flagged this PR" in r.body
    assert "Recorded as approve" in r.body


def test_clean_approve_omits_downgrade_note() -> None:
    r = render_review(_result(verdict="approve", downgraded=False))
    assert "investigator flagged this PR" not in r.body


# ─── Inline comments ────────────────────────────────────────────────


def test_only_will_post_inline_findings_become_inline_comments() -> None:
    findings = [
        _finding(severity="HIGH", start_line=11, will_post_inline=True),
        _finding(severity="MEDIUM", start_line=12, will_post_inline=False),
    ]
    r = render_review(_result(findings=findings))
    assert len(r.inline_comments) == 1
    assert r.inline_comments[0]["start_line"] == 11


def test_inline_comment_carries_severity_category_and_rationale() -> None:
    finding = _finding(
        title="Race in cache fill",
        category="concurrency",
        cited_tool_calls=[
            {"tool": "incident_feedback", "call_id": "x", "summary": "..."}
        ],
    )
    r = render_review(_result(findings=[finding]))
    body = r.inline_comments[0]["body"]
    assert "[HIGH]" in body
    assert "Concurrency / race condition" in body  # label
    assert "concrete production failure mode" in body
    assert "_Cited: incident_feedback_" in body


def test_inline_comment_without_tool_calls_omits_cited_line() -> None:
    finding = _finding(cited_tool_calls=[])
    r = render_review(_result(findings=[finding]))
    body = r.inline_comments[0]["body"]
    assert "Cited:" not in body


def test_inline_comment_deduplicates_repeated_tools() -> None:
    finding = _finding(
        cited_tool_calls=[
            {"tool": "datadog_tool", "call_id": "1", "summary": "x"},
            {"tool": "datadog_tool", "call_id": "2", "summary": "y"},
        ]
    )
    r = render_review(_result(findings=[finding]))
    body = r.inline_comments[0]["body"]
    # Single mention of the tool, despite two cited calls.
    assert body.count("datadog_tool") == 1


def test_inline_comment_carries_end_line_for_multiline_findings() -> None:
    finding = ValidatedFinding(
        severity="HIGH",
        confidence="HIGH",
        category="missing_timeout",
        file_path="foo.py",
        start_line=10,
        end_line=15,
        title="X",
        rationale="Y",
        cited_tool_calls=[],
        will_post_inline=True,
    )
    r = render_review(_result(findings=[finding]))
    assert r.inline_comments[0]["end_line"] == 15


# ─── Rendering is deterministic ─────────────────────────────────────


def test_render_is_pure_function_idempotent() -> None:
    findings = [
        _finding(start_line=11),
        _finding(start_line=12, severity="MEDIUM", will_post_inline=False),
    ]
    a = render_review(_result(findings=findings))
    b = render_review(_result(findings=findings))
    assert a.body == b.body
    assert a.verdict_event == b.verdict_event
    assert a.inline_comments == b.inline_comments


def test_rendered_review_is_dataclass_with_expected_fields() -> None:
    r = render_review(_result())
    assert isinstance(r, RenderedReview)
    assert r.verdict_event in ("APPROVE", "REQUEST_CHANGES")
    assert isinstance(r.body, str) and r.body
    assert isinstance(r.inline_comments, list)
