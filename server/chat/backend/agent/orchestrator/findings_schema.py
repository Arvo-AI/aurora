"""Markdown frontmatter parser and validator for sub-agent findings.md artifacts."""

import yaml
from typing import Optional

# incident_id is intentionally NOT required in findings.md frontmatter — the DB
# column on rca_findings is authoritative. Including it in the LLM-facing schema
# example caused the LLM to copy a literal placeholder or hallucinate a UUID.
_REQUIRED_FRONTMATTER_KEYS = frozenset({
    "agent_id", "purpose", "status",
    "tools_used", "citations", "self_assessed_strength", "follow_ups_suggested",
})
_VALID_STATUSES = frozenset({"succeeded", "failed", "timeout", "cancelled", "inconclusive"})
_VALID_STRENGTHS = frozenset({"strong", "moderate", "weak", "inconclusive"})
_REQUIRED_SECTIONS = frozenset({
    "## Summary", "## Evidence", "## Reasoning", "## What I ruled out",
})


def _split_frontmatter(text: str) -> Optional[tuple[str, str]]:
    """Linear-time split of a `---`-fenced YAML frontmatter from a markdown body.

    Replaces a regex (``^---\\s*\\n(.*?)\\n---\\s*\\n``) flagged by SonarQube as
    backtracking-prone (S5852). Returns ``(yaml_text, body_after_fence)`` if the
    text opens with a ``---`` line and contains a closing ``---`` line; else None.
    """
    lines = text.split("\n")
    if not lines or lines[0].rstrip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].rstrip() == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1:])
    return None


class FindingsValidationError(ValueError):
    """Raised when a findings.md body fails schema validation."""


def _parse_frontmatter(body: str) -> tuple[dict, str]:
    parts = _split_frontmatter(body)
    if parts is None:
        raise FindingsValidationError(
            "findings.md must start with a YAML frontmatter block (--- ... ---)"
        )
    raw_yaml, content = parts
    try:
        meta = yaml.safe_load(raw_yaml)
    except yaml.YAMLError as exc:
        raise FindingsValidationError(f"YAML frontmatter parse error: {exc}") from exc
    if not isinstance(meta, dict):
        raise FindingsValidationError("YAML frontmatter must be a mapping")
    return meta, content


def parse_findings(body: str) -> dict:
    meta, content = _parse_frontmatter(body)
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
    missing = sorted(s for s in _REQUIRED_SECTIONS if s not in content)
    if missing:
        raise FindingsValidationError(
            f"findings.md missing required sections: {missing}"
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
    safe_purpose = (purpose or "see error_message")[:300]
    frontmatter = yaml.safe_dump(
        {
            "agent_id": agent_id,
            "purpose": safe_purpose,
            "status": status,
            "incident_id": incident_id,
            "tools_used": [],
            "citations": [],
            "self_assessed_strength": "inconclusive",
            "follow_ups_suggested": [],
        },
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
    return (
        f"---\n{frontmatter}---\n"
        f"## Summary\n"
        f"Sub-agent {agent_id} ({role_name}) did not complete. Status: {status}.\n"
        f"{safe_error}\n\n"
        f"## Evidence\nNo evidence collected.\n\n"
        f"## Reasoning\nSub-agent terminated before producing findings.\n\n"
        f"## What I ruled out\nNothing ruled out due to early termination.\n"
    )
