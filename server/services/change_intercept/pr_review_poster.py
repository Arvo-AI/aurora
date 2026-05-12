"""Vendor-neutral review body + inline-comment renderers.

Phase 1a's review surface is composed of two pieces submitted in a
single Reviews API call:

    1. Top-level body — a short summary + list of HIGH/MEDIUM findings
       + "Other notes" (LOW) + intent check + advisory disclaimer.
    2. Inline comments — at most ``MAX_INLINE_COMMENTS`` (== 3), one
       per HIGH-severity + HIGH-confidence finding.

This module produces both surfaces as vendor-neutral data: markdown
strings for the bodies, plus a structured list of
``{path, line, end_line, body}`` for the inline comments. The
adapter (Part 3 of the rollout) wraps these in the vendor-specific
API call — for GitHub that's ``POST /pulls/{n}/reviews`` with
``event=APPROVE|REQUEST_CHANGES`` and a ``comments[]`` array.

Why vendor-neutral here: GitLab and Bitbucket adapters can use these
exact renderers; only the wire format of the API call differs. The
validator's :class:`ValidatedFinding` dataclass is already vendor-
neutral so the same input shape feeds every adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .risk_taxonomy import get_category
from .verdict_validator import ValidatedFinding, ValidationResult

# ─── Public renderers ────────────────────────────────────────────────


@dataclass
class RenderedReview:
    """The fully rendered review, vendor-neutral.

    Attributes:
        verdict_event: ``"APPROVE"`` or ``"REQUEST_CHANGES"`` — the
            Reviews-API ``event`` value. Adapters that lack a native
            request-changes state (GitLab) translate this into their
            own idiom in ``post_verdict``.
        body: top-level review body, markdown.
        inline_comments: structured list of
            ``{path, start_line, end_line, body}``. ``end_line`` is
            ``None`` for single-line comments. Empty list when the
            verdict is approve OR when no HIGH+HIGH finding survived
            validation.
    """

    verdict_event: str
    body: str
    inline_comments: list[dict[str, object]] = field(default_factory=list)


def render_review(result: ValidationResult) -> RenderedReview:
    """Render a :class:`ValidationResult` into a vendor-neutral review.

    Idempotent and side-effect free — the same input always produces
    the same output, which matters for the calibration phase where
    we re-render dry-run rows to compare against live runs.

    Args:
        result: validator output. ``findings`` whose
            ``will_post_inline=True`` become inline comments; all
            others land in the body bullets.
    """
    inline_findings = [f for f in result.findings if f.will_post_inline]
    body_findings = [f for f in result.findings if not f.will_post_inline]

    body = _render_body(
        verdict=result.verdict,
        summary=result.summary,
        inline_findings=inline_findings,
        body_findings=body_findings,
        intent_alignment=result.intent_alignment,
        intent_notes=result.intent_notes,
        downgraded_to_approve=result.downgraded_to_approve,
    )

    inline_comments = [
        _render_inline_comment(finding) for finding in inline_findings
    ]

    verdict_event = (
        "REQUEST_CHANGES" if result.verdict == "request_changes" else "APPROVE"
    )
    return RenderedReview(
        verdict_event=verdict_event,
        body=body,
        inline_comments=inline_comments,
    )


# ─── Internal helpers ────────────────────────────────────────────────


_HEADER_REQUEST_CHANGES = "**Aurora flagged production-deployment risk on this PR.**"
_HEADER_APPROVE = "**Aurora reviewed this PR for production-deployment risk.**"
_FOOTER_ADVISORY = (
    "_Aurora is an advisory reviewer in Phase 1a. Dismiss this review to merge anyway._"
)


def _render_body(
    *,
    verdict: str,
    summary: str,
    inline_findings: list[ValidatedFinding],
    body_findings: list[ValidatedFinding],
    intent_alignment: str | None,
    intent_notes: str | None,
    downgraded_to_approve: bool,
) -> str:
    """Compose the top-level review body markdown.

    Section order is fixed so engineers can skim:
        1. Header (verdict-conditional)
        2. Summary
        3. Risks identified (HIGH + MEDIUM in severity order, mirroring inline comments)
        4. Other notes (LOW)
        5. Intent check (only when intent_alignment != matches)
        6. Footer disclaimer
    """
    parts: list[str] = []

    parts.append(
        _HEADER_REQUEST_CHANGES if verdict == "request_changes" else _HEADER_APPROVE
    )
    parts.append("")
    parts.append(summary)

    risks_section = _render_risks_section(inline_findings, body_findings)
    if risks_section:
        parts.append("")
        parts.append(risks_section)

    low_section = _render_low_section(body_findings)
    if low_section:
        parts.append("")
        parts.append(low_section)

    if intent_alignment and intent_alignment != "matches" and intent_notes:
        parts.append("")
        parts.append(f"**Intent check:** {intent_notes}")

    if downgraded_to_approve:
        # When the investigator wanted request_changes but no HIGH+HIGH
        # survived validation, we surface a quiet note so calibration
        # readers can tell apart "clean PR" from "validator-downgraded."
        parts.append("")
        parts.append(
            "_Aurora's investigator flagged this PR but no finding cleared "
            "the HIGH-severity / HIGH-confidence bar. Recorded as approve._"
        )

    parts.append("")
    parts.append(_FOOTER_ADVISORY)
    return "\n".join(parts).strip() + "\n"


def _render_risks_section(
    inline_findings: list[ValidatedFinding],
    body_findings: list[ValidatedFinding],
) -> str:
    """Render the ``### Risks identified`` bullet list.

    Includes HIGH-severity (inline) and MEDIUM-severity findings. LOW
    is rendered separately as "Other notes" so engineers can tell at
    a glance which findings need their attention versus which are
    informational.
    """
    risky = [f for f in inline_findings] + [
        f for f in body_findings if f.severity in ("HIGH", "MEDIUM")
    ]
    if not risky:
        return ""

    lines = ["### Risks identified"]
    for finding in _sorted_for_display(risky):
        lines.append(_render_finding_bullet(finding))
    return "\n".join(lines)


def _render_low_section(body_findings: list[ValidatedFinding]) -> str:
    """Render the ``### Other notes`` bullet list (LOW only)."""
    lows = [f for f in body_findings if f.severity == "LOW"]
    if not lows:
        return ""
    lines = ["### Other notes"]
    for finding in _sorted_for_display(lows):
        lines.append(_render_finding_bullet(finding))
    return "\n".join(lines)


def _render_finding_bullet(finding: ValidatedFinding) -> str:
    """One markdown bullet for the body lists."""
    category_label = _category_label(finding.category)
    location = f"`{finding.file_path}:{finding.start_line}`"
    severity_tag = f"[{finding.severity}]"
    return f"- **{severity_tag} {category_label}** — {location} — {finding.title}"


def _render_inline_comment(finding: ValidatedFinding) -> dict[str, object]:
    """Render one inline comment as ``{path, start_line, end_line, body}``.

    The adapter wraps this into the vendor-native shape — for GitHub
    that's ``{path, line, side: "RIGHT", start_line?, start_side?,
    body}`` in the ``comments`` array of the Reviews API call.
    """
    category_label = _category_label(finding.category)
    citations_line = _render_citations_line(finding)
    body_lines = [
        f"**[{finding.severity}] {category_label}**",
        "",
        finding.rationale,
    ]
    if citations_line:
        body_lines.append("")
        body_lines.append(citations_line)
    return {
        "path": finding.file_path,
        "start_line": finding.start_line,
        "end_line": finding.end_line,
        "body": "\n".join(body_lines),
    }


def _render_citations_line(finding: ValidatedFinding) -> str:
    """Render the trailing ``_Cited: tool1, tool2_`` line for inline
    comments. Returns an empty string when there are no tool calls to
    cite (the ``[diff]`` anchor case)."""
    if not finding.cited_tool_calls:
        return ""
    tool_names = [
        str(c.get("tool"))
        for c in finding.cited_tool_calls
        if isinstance(c, dict) and c.get("tool")
    ]
    if not tool_names:
        return ""
    # Dedup while preserving order so the same tool called twice
    # doesn't read as ``Cited: datadog_tool, datadog_tool``.
    seen: set[str] = set()
    unique: list[str] = []
    for name in tool_names:
        if name not in seen:
            seen.add(name)
            unique.append(name)
    return f"_Cited: {', '.join(unique)}_"


def _category_label(slug: str) -> str:
    """Look up the human label for a category slug. Falls back to the
    slug if for some reason the taxonomy doesn't have an entry (defence
    in depth — the validator already drops unknown categories)."""
    cat = get_category(slug)
    return cat.label if cat else slug


def _sorted_for_display(
    findings: list[ValidatedFinding],
) -> list[ValidatedFinding]:
    """Sort findings deterministically: severity DESC, then file path,
    then line. Stable order matters for the calibration phase where we
    diff dry-run outputs across prompt iterations."""
    severity_rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    return sorted(
        findings,
        key=lambda f: (
            severity_rank.get(f.severity, 99),
            f.file_path,
            f.start_line,
        ),
    )
