"""Markdown frontmatter parser and validator for sub-agent findings.md artifacts."""

import re
import yaml
from typing import Any

_REQUIRED_FRONTMATTER_KEYS = frozenset({
    "agent_id", "purpose", "status", "incident_id",
    "tools_used", "citations", "self_assessed_strength", "follow_ups_suggested",
})
_VALID_STATUSES = frozenset({"succeeded", "failed", "timeout", "cancelled", "inconclusive"})
_VALID_STRENGTHS = frozenset({"strong", "moderate", "weak", "inconclusive"})
_REQUIRED_SECTIONS = frozenset({
    "## Summary", "## Evidence", "## Reasoning", "## What I ruled out",
})

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


class FindingsValidationError(ValueError):
    """Raised when a findings.md body fails schema validation."""


def _parse_frontmatter(body: str) -> tuple[dict, str]:
    match = _FRONTMATTER_RE.match(body)
    if not match:
        raise FindingsValidationError(
            "findings.md must start with a YAML frontmatter block (--- ... ---)"
        )
    raw_yaml = match.group(1)
    try:
        meta = yaml.safe_load(raw_yaml)
    except yaml.YAMLError as exc:
        raise FindingsValidationError(f"YAML frontmatter parse error: {exc}") from exc
    if not isinstance(meta, dict):
        raise FindingsValidationError("YAML frontmatter must be a mapping")
    return meta, body[match.end():]


def parse_findings(body: str) -> dict:
    meta, _ = _parse_frontmatter(body)
    missing = _REQUIRED_FRONTMATTER_KEYS - meta.keys()
    if missing:
        raise FindingsValidationError(
            f"findings.md frontmatter missing required keys: {sorted(missing)}"
        )
    status = str(meta.get("status", ""))
    if status not in _VALID_STATUSES:
        raise FindingsValidationError(
            f"findings.md status must be one of {sorted(_VALID_STATUSES)}, got {status!r}"
        )
    strength = str(meta.get("self_assessed_strength", ""))
    if strength not in _VALID_STRENGTHS:
        raise FindingsValidationError(
            f"findings.md self_assessed_strength must be one of {sorted(_VALID_STRENGTHS)}, got {strength!r}"
        )
    for section in _REQUIRED_SECTIONS:
        if section not in body:
            raise FindingsValidationError(
                f"findings.md missing required section: {section!r}"
            )
    for list_key in ("tools_used", "citations", "follow_ups_suggested"):
        if not isinstance(meta.get(list_key), list):
            raise FindingsValidationError(
                f"findings.md frontmatter key {list_key!r} must be a YAML list"
            )
    return meta


def make_stub(agent_id: str, role_name: str, incident_id: str, purpose: str,
              status: str, error_message: str = "") -> str:
    if status not in _VALID_STATUSES:
        status = "failed"
    safe_error = (error_message or "")[:500].replace("\n", " ")
    safe_purpose = (purpose or "see error_message").replace('"', "'")[:300]
    return f"""---
agent_id: {agent_id}
purpose: "{safe_purpose}"
status: {status}
incident_id: {incident_id}
tools_used: []
citations: []
self_assessed_strength: inconclusive
follow_ups_suggested: []
---
## Summary
Sub-agent {agent_id} ({role_name}) did not complete. Status: {status}.
{safe_error}

## Evidence
No evidence collected.

## Reasoning
Sub-agent terminated before producing findings.

## What I ruled out
Nothing ruled out due to early termination.
"""
