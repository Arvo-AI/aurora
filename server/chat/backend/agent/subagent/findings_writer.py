"""Schema-enforced findings.md writer for sub-agents."""
from __future__ import annotations

import io
import logging
from datetime import datetime, timezone
from typing import Any

import yaml

from utils.storage.storage import get_storage_manager

from chat.backend.agent.subagent.state import SubAgentState

logger = logging.getLogger(__name__)


REQUIRED_FRONTMATTER_KEYS = (
    "agent_id",
    "purpose",
    "status",
    "incident_id",
    "started_at",
    "ended_at",
    "tools_used",
    "citations",
)

REQUIRED_SECTIONS = ("Summary", "Evidence", "Reasoning", "What I ruled out")

_VALID_STRENGTH = {"high", "medium", "low", "inconclusive"}


class FindingsSchemaError(ValueError):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _format_evidence(evidence: list[dict[str, Any]]) -> str:
    if not evidence:
        return "(no evidence captured)"
    lines: list[str] = []
    for item in evidence:
        bullet = item.get("text") or item.get("summary") or ""
        cite = item.get("citation")
        if cite:
            lines.append(f"- {bullet} [{cite}]")
        else:
            lines.append(f"- {bullet}")
    return "\n".join(lines)


def _format_ruled_out(ruled_out: list[str]) -> str:
    if not ruled_out:
        return "(nothing ruled out)"
    return "\n".join(f"- {item}" for item in ruled_out)


def _validate(frontmatter: dict[str, Any], sections: dict[str, str]) -> None:
    missing_keys = [k for k in REQUIRED_FRONTMATTER_KEYS if k not in frontmatter or frontmatter[k] in (None, "")]
    if missing_keys:
        raise FindingsSchemaError(f"frontmatter missing keys: {missing_keys}")

    missing_sections = [s for s in REQUIRED_SECTIONS if not sections.get(s, "").strip()]
    if missing_sections:
        raise FindingsSchemaError(f"sections missing or empty: {missing_sections}")

    strength = frontmatter.get("self_assessed_strength")
    if strength not in _VALID_STRENGTH:
        raise FindingsSchemaError(
            f"self_assessed_strength must be one of {_VALID_STRENGTH}, got {strength!r}"
        )


def _render(frontmatter: dict[str, Any], sections: dict[str, str]) -> str:
    fm_yaml = yaml.safe_dump(frontmatter, sort_keys=False).strip()
    body_parts = [f"## {name}\n{sections[name]}" for name in REQUIRED_SECTIONS]
    return f"---\n{fm_yaml}\n---\n\n" + "\n\n".join(body_parts) + "\n"


def _artifact_path(state: SubAgentState) -> str:
    org = state.org_id or "_no_org"
    incident = state.incident_id or "_no_incident"
    return f"subagent_findings/{org}/{incident}/{state.agent_id}.md"


def write_findings(
    state: SubAgentState,
    summary: str,
    evidence: list[dict],
    reasoning: str,
    ruled_out: list[str],
    citations: list[str],
    self_assessed_strength: str = "medium",
) -> str:
    started_at = _now_iso()
    ended_at = started_at

    frontmatter: dict[str, Any] = {
        "agent_id": state.agent_id,
        "purpose": state.purpose,
        "status": state.status if state.status in ("succeeded", "failed", "cancelled") else "succeeded",
        "incident_id": state.incident_id or "",
        "started_at": started_at,
        "ended_at": ended_at,
        "tools_used": list(state.tools_used or []),
        "citations": list(citations or []),
        "self_assessed_strength": self_assessed_strength,
        "follow_ups_suggested": [],
    }

    sections = {
        "Summary": (summary or "").strip(),
        "Evidence": _format_evidence(evidence or []),
        "Reasoning": (reasoning or "").strip(),
        "What I ruled out": _format_ruled_out(ruled_out or []),
    }

    _validate(frontmatter, sections)

    markdown = _render(frontmatter, sections)
    path = _artifact_path(state)

    storage = get_storage_manager()
    storage.upload_bytes(
        data=markdown.encode("utf-8"),
        path=path,
        content_type="text/markdown",
    )
    return path
