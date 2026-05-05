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
    f"Maximum {_MAX_TURNS_DEFAULT} tool-calling turns. Budget each turn carefully — leave at least one turn for `write_findings`.",
    "Your investigation ENDS ONLY when you call `write_findings`. Do NOT reply with plain text under any circumstance — every response must be either a tool call or a `write_findings` call.",
    "If a tool errors, returns no data, or you cannot make further progress, call `write_findings` immediately with `status: inconclusive` and explain in the Reasoning section what you tried.",
    "Even with zero findings, call `write_findings` with a populated body (status: inconclusive) — never just stop.",
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
    status: Literal["succeeded", "failed", "timeout", "cancelled", "inconclusive"]
    self_assessed_strength: Optional[Literal["strong", "moderate", "weak", "inconclusive"]] = None
    error_message: Optional[str] = None
    wave: Optional[int] = None
    summary: Optional[str] = None
    tool_call_history: list = []


_CLOUD_PROVIDERS = frozenset({"aws", "gcp", "azure", "ovh", "scaleway"})


def render_brief(
    inp: SubAgentInput,
    role_meta: Any,
    connected_providers: Optional[list[str]] = None,
) -> str:
    """Return the system-prompt brief the sub-agent receives.

    ``role_meta`` is a ``RoleMeta`` object from ``role_registry.py``.
    ``connected_providers`` is the list of provider/skill ids the user has
    connected (e.g. ``['gcp', 'github', 'datadog']``). When supplied, the brief
    constrains the sub-agent to only use those providers — prevents the LLM
    from defaulting to AWS or other unconnected providers it learned about
    in pre-training.

    The return value is pure markdown — no secrets.
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

    if connected_providers:
        cloud = sorted(p for p in connected_providers if p.lower() in _CLOUD_PROVIDERS)
        other = sorted(p for p in connected_providers if p.lower() not in _CLOUD_PROVIDERS)
        lines += ["", "## Available Tooling Context"]
        lines.append(f"- Connected providers: {', '.join(sorted(connected_providers))}")
        if cloud:
            lines.append(
                f"- When calling `cloud_exec`, you MUST use `provider` from this set ONLY: "
                f"{', '.join(cloud)}. Other cloud providers are NOT connected and will fail."
            )
        else:
            lines.append(
                "- No cloud providers are connected. Do NOT call `cloud_exec` — it will fail."
            )
        if other:
            lines.append(f"- Other connected integrations: {', '.join(other)}")

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
