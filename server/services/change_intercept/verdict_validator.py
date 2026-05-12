"""Validator for investigator output.

The investigator emits a JSON document of the shape:

    {
        "verdict": "approve" | "request_changes",
        "summary": "...",
        "intent_alignment": "matches" | "partial" | "mismatch",
        "intent_notes": "..." | null,
        "findings": [
            {
                "severity": "HIGH" | "MEDIUM" | "LOW",
                "confidence": "HIGH" | "MEDIUM" | "LOW",
                "category": "<slug from risk_taxonomy>",
                "file_path": "...",
                "start_line": 42,
                "end_line": 47 | null,
                "title": "...",
                "rationale": "...",
                "cited_tool_calls": [{"tool": "...", "call_id": "...", "summary": "..."}]
            },
            ...
        ]
    }

The validator's job is to enforce the Phase 1a invariants and produce
the *trusted* findings list that the review-poster ultimately renders.
It is deliberately conservative: anything that looks shaky gets
dropped or downgraded. We would rather under-block than cry wolf
during the first weeks of advisory reviews.

Per the resolved open questions (#1, #2 in the discussion):

  - Inline comments fire ONLY on ``severity=HIGH`` AND ``confidence=HIGH``.
    Findings that don't clear both bars stay in the top-level body
    (MEDIUM-severity) or are dropped (LOW-severity).
  - At most 3 inline comments per investigation. If more than 3
    findings qualify, keep the top 3 by ``(severity, confidence,
    category-criticality)`` and roll the rest into a body line.
  - ``request_changes`` requires ≥1 HIGH-severity + HIGH-confidence
    finding. Otherwise approve.

The validator never raises on bad input — pathological investigator
output (truncated JSON, unknown fields, wrong types) results in a
``ValidationResult`` with ``verdict='approve'``, an empty findings
list, and the failure reason in ``drop_log``. The Celery task
persists this safely as a dry-run row.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .diff_parser import DiffIndex, parse_unified_diff
from .risk_taxonomy import CATEGORIES_BY_SLUG

logger = logging.getLogger(__name__)


# ─── Configuration constants ─────────────────────────────────────────


# Per the resolved open question on comment volume: 3 is the hard cap.
# Bumping this means revisiting "shouldn't be too many comments per
# review" with the customer — the cap is a UX contract, not a
# performance limit.
MAX_INLINE_COMMENTS: int = 3

# Enum sets accepted by the validator. ``None`` values land here as
# the literal string "none" so a single set check covers every case.
_VALID_VERDICTS: frozenset[str] = frozenset({"approve", "request_changes"})
_VALID_SEVERITIES: frozenset[str] = frozenset({"HIGH", "MEDIUM", "LOW"})
_VALID_CONFIDENCES: frozenset[str] = frozenset({"HIGH", "MEDIUM", "LOW"})
_VALID_INTENT: frozenset[str] = frozenset({"matches", "partial", "mismatch"})

# Severity rank: HIGH > MEDIUM > LOW. Used for inline-comment top-N
# selection and dedup tiebreak.
_SEVERITY_RANK: dict[str, int] = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
_CONFIDENCE_RANK: dict[str, int] = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}

# Category-criticality used as a tertiary tiebreaker when two findings
# share severity + confidence. Roughly ordered by "would a postmortem
# author write this up" — production-shaping bugs first, code-quality
# adjacent risks last.
_CATEGORY_CRITICALITY: dict[str, int] = {
    "dangerous_config": 12,
    "unsafe_migration": 11,
    "secret_handling": 10,
    "memory_leak": 9,
    "missing_timeout": 8,
    "unbounded_retry": 7,
    "concurrency": 6,
    "blocking_in_hot_path": 5,
    "error_swallowing": 4,
    "breaking_api_change": 3,
    "n_plus_one": 2,
    "dependency_risk": 1,
}


# ─── Result types ────────────────────────────────────────────────────


@dataclass
class ValidatedFinding:
    """A single finding after validation passes.

    All fields are guaranteed-valid (severity / confidence / category
    in their enums; ``file_path`` references a diff hunk;
    ``start_line`` anchors at a changed line). The Celery task
    serialises this to ``change_investigations.findings`` JSONB.

    Attributes:
        severity: ``HIGH`` / ``MEDIUM`` / ``LOW``.
        confidence: ``HIGH`` / ``MEDIUM`` / ``LOW`` — may have been
            downgraded by the validator if the finding cited no
            evidence the validator could verify mechanically.
        category: slug from ``risk_taxonomy.CATEGORIES_BY_SLUG``.
        file_path: post-change path of the file the finding points at.
        start_line: 1-indexed new-file line number; must be in the
            diff index.
        end_line: optional end of a multi-line range (``None`` for
            single-line findings).
        title: one-liner header used by the review poster.
        rationale: 2-3 sentence explanation of the production
            failure mode.
        cited_tool_calls: passthrough of the investigator's citations.
            Empty list is permitted only when the rationale contains
            an explicit ``[diff]`` reference (mechanically verified).
        will_post_inline: derived flag — only HIGH-severity +
            HIGH-confidence findings make it past the inline-cap
            selection and have this set to ``True``.
    """

    severity: str
    confidence: str
    category: str
    file_path: str
    start_line: int
    end_line: int | None
    title: str
    rationale: str
    cited_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    will_post_inline: bool = False


@dataclass
class DroppedFinding:
    """An investigator-emitted finding that didn't survive validation.

    Persisted to ``change_investigations.dropped_findings`` so the
    calibration phase can see *why* findings get dropped and tune the
    prompt accordingly. Each instance carries a short ``reason`` from
    ``DROP_REASONS``.
    """

    reason: str
    raw: dict[str, Any]


@dataclass
class ValidationResult:
    """Validator output passed to the Celery task for persistence.

    Attributes:
        verdict: final verdict after reconciliation
            (``approve`` if no HIGH+HIGH findings survived).
        summary: ≤2-sentence summary for the top-level review body.
        intent_alignment: ``matches`` / ``partial`` / ``mismatch`` /
            ``None``.
        intent_notes: short note when ``intent_alignment != 'matches'``.
        findings: surviving findings, with ``will_post_inline`` set
            on at most ``MAX_INLINE_COMMENTS`` of them.
        dropped: findings that didn't make it, with drop reasons.
        downgraded_to_approve: ``True`` when the investigator said
            ``request_changes`` but no HIGH+HIGH finding survived and
            we downgraded to ``approve`` (under-block bias).
        drop_log: human-readable summary appended to logs for ops
            visibility; not persisted.
    """

    verdict: str
    summary: str
    intent_alignment: str | None
    intent_notes: str | None
    findings: list[ValidatedFinding]
    dropped: list[DroppedFinding] = field(default_factory=list)
    downgraded_to_approve: bool = False
    drop_log: list[str] = field(default_factory=list)


# Drop-reason vocabulary. Stable strings — these are persisted to
# ``dropped_findings.reason`` and used in calibration dashboards.
DROP_REASONS = (
    "schema_invalid",
    "unknown_category",
    "bad_severity",
    "bad_confidence",
    "missing_path",
    "missing_line",
    "unanchored_line",
    "no_citation_no_diff_anchor",
    "duplicate",
)


# ─── Top-level entry point ───────────────────────────────────────────


def validate(
    raw_output: Any,
    diff_text: str,
) -> ValidationResult:
    """Validate raw investigator output against ``diff_text``.

    Args:
        raw_output: parsed JSON from the investigator. Anything that
            isn't a dict yields the safe-default approve result.
        diff_text: unified diff stored on ``change_events.change_diff``.
            Used to anchor each finding at a real changed line.

    Returns:
        A :class:`ValidationResult` with surviving findings, dropped
        findings (with reasons), and the reconciled verdict. The
        result is always well-formed — no exceptions escape this
        function.
    """
    if not isinstance(raw_output, dict):
        return _safe_default(reason="schema_invalid: not a dict")

    diff_index = parse_unified_diff(diff_text or "")

    # ─── Top-level fields ───
    verdict_in = _coerce_str(raw_output.get("verdict"))
    if verdict_in not in _VALID_VERDICTS:
        return _safe_default(reason=f"schema_invalid: bad verdict {verdict_in!r}")

    summary = _coerce_str(raw_output.get("summary"))
    if not summary:
        # Empty summary is recoverable — render a generic placeholder.
        summary = "Aurora reviewed this PR for production-deployment risk."

    intent_alignment = _coerce_str(raw_output.get("intent_alignment")) or None
    if intent_alignment and intent_alignment not in _VALID_INTENT:
        intent_alignment = None
    intent_notes = _coerce_str(raw_output.get("intent_notes")) or None
    # An "intent matches" finding doesn't carry notes; clear if mismatched.
    if intent_alignment == "matches":
        intent_notes = None

    # ─── Findings ───
    findings_in = raw_output.get("findings") or []
    if not isinstance(findings_in, list):
        findings_in = []

    survivors: list[ValidatedFinding] = []
    dropped: list[DroppedFinding] = []

    for raw_finding in findings_in:
        if not isinstance(raw_finding, dict):
            dropped.append(DroppedFinding(reason="schema_invalid", raw={"value": raw_finding}))
            continue
        result = _validate_finding(raw_finding, diff_index)
        if isinstance(result, ValidatedFinding):
            survivors.append(result)
        else:
            dropped.append(result)

    # ─── Dedup before cap ───
    survivors, deduped = _dedup_findings(survivors)
    dropped.extend(deduped)

    # ─── Cap inline comments to MAX_INLINE_COMMENTS ───
    _select_inline_findings(survivors)

    # ─── Verdict reconciliation ───
    has_high_high = any(
        f.severity == "HIGH" and f.confidence == "HIGH" for f in survivors
    )
    downgraded = False
    if verdict_in == "request_changes" and not has_high_high:
        verdict_in = "approve"
        downgraded = True
        # If we downgrade we deliberately leave intent_notes intact —
        # the review-poster surfaces it under "Intent check" without
        # blocking the merge.

    drop_log = _build_drop_log(dropped)

    return ValidationResult(
        verdict=verdict_in,
        summary=summary,
        intent_alignment=intent_alignment,
        intent_notes=intent_notes,
        findings=survivors,
        dropped=dropped,
        downgraded_to_approve=downgraded,
        drop_log=drop_log,
    )


# ─── Internal helpers ────────────────────────────────────────────────


def _safe_default(reason: str) -> ValidationResult:
    """Build a guaranteed-safe ValidationResult for catastrophic input.

    Used when the top-level shape is unusable. Returns ``approve`` so
    the dispatcher persists a benign dry-run row and the calibration
    pipeline still gets a sample.
    """
    return ValidationResult(
        verdict="approve",
        summary="Aurora was unable to parse the investigator output (dry-run dropped).",
        intent_alignment=None,
        intent_notes=None,
        findings=[],
        dropped=[],
        downgraded_to_approve=False,
        drop_log=[reason],
    )


def _validate_finding(
    raw: dict[str, Any],
    diff_index: DiffIndex,
) -> ValidatedFinding | DroppedFinding:
    """Apply per-finding checks. Returns a survivor or a drop record."""

    severity = _coerce_str(raw.get("severity")).upper()
    if severity not in _VALID_SEVERITIES:
        return DroppedFinding(reason="bad_severity", raw=raw)

    confidence = _coerce_str(raw.get("confidence")).upper()
    if confidence not in _VALID_CONFIDENCES:
        # Default to MEDIUM rather than dropping — confidence is the
        # less-load-bearing of the two enums (severity gates verdict,
        # confidence only gates inline-posting).
        confidence = "MEDIUM"

    category = _coerce_str(raw.get("category"))
    if category not in CATEGORIES_BY_SLUG:
        return DroppedFinding(reason="unknown_category", raw=raw)

    file_path = _coerce_str(raw.get("file_path"))
    if not file_path:
        return DroppedFinding(reason="missing_path", raw=raw)

    start_line = _coerce_int(raw.get("start_line"))
    if start_line is None or start_line <= 0:
        return DroppedFinding(reason="missing_line", raw=raw)
    end_line = _coerce_int(raw.get("end_line"))
    if end_line is not None and end_line < start_line:
        end_line = None

    if not diff_index.is_changed_line(file_path, start_line):
        # Tolerate a lax-anchor: if the line falls anywhere inside a
        # hunk we touched (e.g. context line adjacent to a changed
        # line), keep it but downgrade confidence. Pure unanchored
        # references get dropped.
        if diff_index.is_in_hunk(file_path, start_line):
            confidence = _downgrade_confidence(confidence)
        else:
            return DroppedFinding(reason="unanchored_line", raw=raw)

    title = _coerce_str(raw.get("title")) or "Risk noted"
    rationale = _coerce_str(raw.get("rationale")) or ""

    cited_tool_calls = raw.get("cited_tool_calls") or []
    if not isinstance(cited_tool_calls, list):
        cited_tool_calls = []
    cited_tool_calls = [c for c in cited_tool_calls if isinstance(c, dict)]

    if not cited_tool_calls and "[diff]" not in rationale.lower():
        # The Phase 1a contract requires ≥1 citation OR an explicit
        # ``[diff]`` mechanical-anchor in the rationale. Without either,
        # the finding is hand-wavy — drop.
        return DroppedFinding(reason="no_citation_no_diff_anchor", raw=raw)

    return ValidatedFinding(
        severity=severity,
        confidence=confidence,
        category=category,
        file_path=file_path,
        start_line=start_line,
        end_line=end_line,
        title=title,
        rationale=rationale,
        cited_tool_calls=cited_tool_calls,
    )


def _downgrade_confidence(confidence: str) -> str:
    """HIGH → MEDIUM; MEDIUM → LOW; LOW → LOW."""
    if confidence == "HIGH":
        return "MEDIUM"
    if confidence == "MEDIUM":
        return "LOW"
    return "LOW"


def _dedup_findings(
    findings: list[ValidatedFinding],
) -> tuple[list[ValidatedFinding], list[DroppedFinding]]:
    """Drop duplicate findings at the same ``(path, line, category)``.

    When two findings collide, keep the one with the higher
    ``(severity_rank, confidence_rank, category_criticality)`` tuple.
    The losing duplicates land in ``dropped`` with reason ``duplicate``.
    """
    by_key: dict[tuple[str, int, str], ValidatedFinding] = {}
    dropped: list[DroppedFinding] = []

    for finding in findings:
        key = (finding.file_path, finding.start_line, finding.category)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = finding
            continue
        # Tiebreak.
        if _finding_rank(finding) > _finding_rank(existing):
            dropped.append(_to_dropped(existing, "duplicate"))
            by_key[key] = finding
        else:
            dropped.append(_to_dropped(finding, "duplicate"))

    # Preserve insertion order of survivors for downstream determinism.
    seen_keys: set[tuple[str, int, str]] = set()
    survivors: list[ValidatedFinding] = []
    for finding in findings:
        key = (finding.file_path, finding.start_line, finding.category)
        if key in seen_keys:
            continue
        if by_key.get(key) is finding:
            survivors.append(finding)
            seen_keys.add(key)
    return survivors, dropped


def _to_dropped(finding: ValidatedFinding, reason: str) -> DroppedFinding:
    raw = {
        "severity": finding.severity,
        "confidence": finding.confidence,
        "category": finding.category,
        "file_path": finding.file_path,
        "start_line": finding.start_line,
        "end_line": finding.end_line,
        "title": finding.title,
        "rationale": finding.rationale,
        "cited_tool_calls": list(finding.cited_tool_calls),
    }
    return DroppedFinding(reason=reason, raw=raw)


def _finding_rank(finding: ValidatedFinding) -> tuple[int, int, int]:
    """Return a comparable rank tuple. Higher is better."""
    return (
        _SEVERITY_RANK.get(finding.severity, 0),
        _CONFIDENCE_RANK.get(finding.confidence, 0),
        _CATEGORY_CRITICALITY.get(finding.category, 0),
    )


def _select_inline_findings(findings: list[ValidatedFinding]) -> None:
    """Mark up to ``MAX_INLINE_COMMENTS`` findings for inline posting.

    Only ``severity=HIGH`` AND ``confidence=HIGH`` are eligible per
    Phase 1a policy. Ranking: rank tuple from :func:`_finding_rank`,
    then original insertion order as a final tiebreak.

    The function mutates ``findings`` in place — sets
    ``will_post_inline=True`` on the selected entries.
    """
    eligible = [
        (idx, f)
        for idx, f in enumerate(findings)
        if f.severity == "HIGH" and f.confidence == "HIGH"
    ]
    if not eligible:
        return

    # Sort descending by rank, ascending by insertion idx as tiebreak.
    eligible.sort(key=lambda pair: (_finding_rank(pair[1]), -pair[0]), reverse=True)
    for _, finding in eligible[:MAX_INLINE_COMMENTS]:
        finding.will_post_inline = True


def _build_drop_log(dropped: list[DroppedFinding]) -> list[str]:
    """Aggregate drop reasons into a compact summary for ops logging."""
    counts: dict[str, int] = {}
    for d in dropped:
        counts[d.reason] = counts.get(d.reason, 0) + 1
    return [f"{reason}={count}" for reason, count in sorted(counts.items())]


# ─── Tiny coercion helpers ──────────────────────────────────────────


def _coerce_str(value: Any) -> str:
    """Return ``value`` as a stripped string, or empty for non-strings."""
    if isinstance(value, str):
        return value.strip()
    return ""


def _coerce_int(value: Any) -> int | None:
    """Return ``value`` as an int, or ``None`` for invalid input.

    Accepts ``int`` (passes through), str-numerics (``"42"``), and
    rejects floats / None / booleans / non-numeric strings.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None
