"""Reads + validates a sub-agent's findings.md against the schema."""
from __future__ import annotations

import logging
import re
from typing import Any

import yaml

from utils.storage.storage import get_storage_manager

logger = logging.getLogger(__name__)


REQUIRED_FRONTMATTER_KEYS = {
    "agent_id",
    "purpose",
    "status",
    "incident_id",
    "started_at",
    "ended_at",
    "tools_used",
    "citations",
}

REQUIRED_SECTIONS = ("Summary", "Evidence", "Reasoning", "What I ruled out")

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL)
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


class FindingsValidationError(ValueError):
    pass


def _split_sections(body: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    matches = list(_SECTION_RE.finditer(body))
    for i, m in enumerate(matches):
        name = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        sections[name] = body[start:end].strip()
    return sections


def _parse(markdown: str) -> tuple[dict[str, Any], str]:
    m = _FRONTMATTER_RE.match(markdown)
    if not m:
        raise FindingsValidationError("findings.md missing YAML frontmatter block")
    try:
        frontmatter = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        raise FindingsValidationError(f"frontmatter is not valid YAML: {e}") from e
    if not isinstance(frontmatter, dict):
        raise FindingsValidationError("frontmatter must be a mapping")
    return frontmatter, m.group(2)


def read_findings(artifact_ref: str) -> dict:
    storage = get_storage_manager()
    raw = storage.download_bytes(artifact_ref)
    markdown = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)

    frontmatter, body = _parse(markdown)

    missing = REQUIRED_FRONTMATTER_KEYS - set(frontmatter.keys())
    if missing:
        raise FindingsValidationError(
            f"frontmatter missing required keys: {sorted(missing)}"
        )

    sections = _split_sections(body)
    missing_sections = [s for s in REQUIRED_SECTIONS if s not in sections]
    if missing_sections:
        raise FindingsValidationError(
            f"findings.md missing required sections: {missing_sections}"
        )

    return {"frontmatter": frontmatter, "body": body, "sections": sections}
