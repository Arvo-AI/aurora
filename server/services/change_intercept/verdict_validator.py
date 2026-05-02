"""Validate and normalize the investigator's structured verdict output.

The LLM is instructed to emit a JSON block with a binary verdict.
This module parses that output, enforces the citation rule (a
``request_changes`` without cited findings is downgraded to ``approve``),
and returns a clean dict ready for persistence.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_VALID_VERDICTS = frozenset({"approve", "request_changes"})
_VALID_ALIGNMENTS = frozenset({"matches", "partial", "mismatch"})

_JSON_BLOCK_RE = re.compile(
    r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL
)
_BARE_JSON_RE = re.compile(
    r'(\{\s*"verdict"\s*:.*?\})', re.DOTALL
)


def _extract_json(raw: str) -> dict[str, Any] | None:
    """Best-effort extraction of the verdict JSON from LLM output."""
    for pattern in (_JSON_BLOCK_RE, _BARE_JSON_RE):
        m = pattern.search(raw)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def validate_verdict(raw_output: str) -> dict[str, Any]:
    """Parse, validate, and normalize a verdict from raw LLM output.

    Returns a dict with keys: verdict, rationale, intent_alignment,
    intent_notes, cited_findings.

    Raises ValueError when the output is completely unparseable.
    """
    parsed = _extract_json(raw_output)
    if parsed is None:
        raise ValueError("Could not parse verdict JSON from investigator output")

    verdict = str(parsed.get("verdict", "")).strip().lower()
    if verdict not in _VALID_VERDICTS:
        raise ValueError(f"Invalid verdict {verdict!r}; expected one of {_VALID_VERDICTS}")

    rationale = str(parsed.get("rationale", "")).strip()
    intent_alignment = str(parsed.get("intent_alignment", "")).strip().lower() or None
    if intent_alignment and intent_alignment not in _VALID_ALIGNMENTS:
        intent_alignment = None
    intent_notes = parsed.get("intent_notes") or None
    cited_findings: list[dict[str, Any]] = parsed.get("cited_findings") or []

    if verdict == "request_changes" and not cited_findings:
        logger.warning(
            "[VerdictValidator] request_changes without citations — "
            "downgrading to approve"
        )
        verdict = "approve"

    return {
        "verdict": verdict,
        "rationale": rationale,
        "intent_alignment": intent_alignment,
        "intent_notes": intent_notes,
        "cited_findings": cited_findings,
    }
