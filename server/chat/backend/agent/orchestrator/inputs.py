"""Sub-agent input/output models and brief renderer for the multi-agent RCA orchestrator."""

from typing import Optional, Literal, Any
from pydantic import BaseModel, ConfigDict

# ---------------------------------------------------------------------------
# V1 hardcoded constants
# ---------------------------------------------------------------------------
_MAX_TURNS_DEFAULT = 8
_REQUIRED_FINDINGS_SECTIONS = ["## Summary", "## Evidence", "## Reasoning", "## What I ruled out"]
_HARD_CONSTRAINTS = [
    "You are READ-ONLY. Never call any tool marked as mutating.",
    f"Maximum {_MAX_TURNS_DEFAULT} turns. Budget each turn carefully.",
    "You MUST end by calling the `write_findings` tool with a valid findings.md body.",
    "findings.md must have YAML frontmatter followed by ## Summary, ## Evidence, ## Reasoning, ## What I ruled out.",
]


class SubAgentInput(BaseModel):
    """All inputs a sub-agent needs to begin its investigation."""

    agent_id: str
    role_name: str
    purpose: str
    time_window: Optional[str] = None
    evidence_refs: list[str] = []
    extra_constraints: Optional[dict] = None

    # strict — reject unknown keys from LLM output
    model_config = ConfigDict(extra="forbid")


class FindingRef(BaseModel):
    """Lightweight reference written back to main state after a sub-agent completes."""

    agent_id: str
    role_name: str
    storage_uri: Optional[str]
    status: Literal["succeeded", "failed", "timeout", "cancelled"]
    self_assessed_strength: Optional[Literal["strong", "moderate", "weak", "inconclusive"]] = None
    error_message: Optional[str] = None
    wave: Optional[int] = None
    summary: Optional[str] = None
    tool_call_history: list = []


def render_brief(inp: SubAgentInput, role_meta: Any) -> str:
    """Return the system-prompt brief the sub-agent receives.

    ``role_meta`` is a ``RoleMeta`` object from ``role_registry.py``.
    The return value is pure markdown — no secrets, no provider names.
    """
    lines: list[str] = [
        f"# Role: {role_meta.name}",
        "",
        role_meta.description,
        "",
        "## Your Task",
        f"**Purpose (bounded scope):** {inp.purpose}",
    ]

    if inp.time_window:
        lines += ["", f"**Time window to investigate:** {inp.time_window}"]

    if inp.evidence_refs:
        lines += ["", "## Evidence References to Consult"]
        for ref in inp.evidence_refs:
            lines.append(f"- {ref}")

    lines += [
        "",
        "## Hard Constraints",
    ]
    for c in _HARD_CONSTRAINTS:
        lines.append(f"- {c}")

    if inp.extra_constraints:
        for k, v in inp.extra_constraints.items():
            lines.append(f"- {k}: {v}")

    lines += [
        "",
        "## Required findings.md Schema",
        "```yaml",
        "---",
        f"agent_id: {inp.agent_id}",
        f"purpose: {inp.purpose}",
        "status: succeeded|failed|timeout|cancelled|inconclusive",
        "incident_id: <incident UUID>",
        "tools_used: []",
        "citations: []",
        "self_assessed_strength: strong|moderate|weak|inconclusive",
        "follow_ups_suggested:",
        "  - role: <role_name>",
        "    purpose: <one sentence>",
        "    time_window: <optional>",
        "---",
        "## Summary",
        "## Evidence",
        "## Reasoning",
        "## What I ruled out",
        "```",
    ]

    return "\n".join(lines)
