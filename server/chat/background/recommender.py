"""Next Steps v3: generate suggestions from the investigation trace.

The Recommender consumes raw investigation evidence (tool calls + outputs + agent
reasoning), optionally executes safe read-only diagnostics, then generates
suggestions that actually require a human — following the Three-Class Invariant:

  1. Privilege gap — Aurora lacks tooling/access the human has
  2. Non-trivial risk — requires human judgment/authorization
  3. Human knowledge — requires org context or a decision

If a suggestion is risk=safe AND Aurora has the tooling, it must not appear as
a suggestion. Aurora must have run it (or run it in the self-execution round).
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, List, Optional

from langchain_core.messages import HumanMessage

from chat.background.citation_extractor import Citation
from chat.background.suggestion_extractor import Suggestion, is_command_safe

from chat.background.citation_extractor import _TOOL_NAME_MAPPING

logger = logging.getLogger(__name__)

_MAX_TRACE_CHARS = 30_000
_MAX_REASONING_CHARS = 6_000
_RECENT_OUTPUT_CHARS = 600
_OLDER_OUTPUT_CHARS = 200
_CODE_OUTPUT_CHARS = 2000

_CODE_TOOLS = frozenset({
    "GitHub RCA", "MCP: Get Commit",
    "MCP: List Commits", "MCP: List Pull Requests",
})

_VALID_TYPES = frozenset({"mitigation", "diagnostic", "remediate", "prevent"})
_VALID_RISKS = frozenset({"safe", "low", "medium", "high"})
_ENRICHMENT_MODEL = os.environ.get("ENRICHMENT_MODEL", "anthropic/claude-haiku-4.5")

_TYPE_SORT_ORDER = {"mitigation": 0, "diagnostic": 1, "remediate": 2, "prevent": 3}

_SECTION_END_MARKERS = ["\n```", "\n---", "\n# ", "\n### ", "\nLet me", "\nI'll ", "\nI will "]

# Max self-execution round tool calls
_MAX_SELF_EXEC_CALLS = 5
_SELF_EXEC_TIMEOUT = 30  # seconds per command


def _strip_code_fences(text: str) -> str:
    """Remove markdown code fences wrapping JSON output."""
    if text.startswith("```"):
        lines = text.split("\n")
        end_index = -1 if lines[-1].strip() == "```" else len(lines)
        return "\n".join(lines[1:end_index]).strip()
    return text


# ---------------------------------------------------------------------------
# Hypothesis ledger — extracted from agent reasoning
# ---------------------------------------------------------------------------

@dataclass
class Hypothesis:
    statement: str
    probability: str  # "high" | "medium" | "low"
    supporting_evidence: List[str] = field(default_factory=list)
    contradicting_evidence: List[str] = field(default_factory=list)
    mitigation_class: str = "unknown"  # rollback | restart | scale | config_revert | code_fix | unknown
    status: str = "open"  # confirmed | open | ruled_out


@dataclass
class InvestigationState:
    hypotheses: List[Hypothesis] = field(default_factory=list)
    ruled_out: List[Hypothesis] = field(default_factory=list)
    unexplored: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------



_INTERNAL_REF_RE = re.compile(
    r"(?:suggestion\s+id[:\s]*\d+|RCA-\d+|tool\s+call\s*\[\d+\])", re.IGNORECASE
)


_REPO_ACCESS_TOOLS = {"MCP: Get File Contents", "MCP: Get Commit", "MCP: List Commits",
                      "MCP: Create Or Update File", "GitHub RCA"}

_RESOURCE_PATTERNS = [
    ("files", r'(?:^|[\s"/])([a-zA-Z][\w./]*\.(?:go|py|ts|js|yaml|yml|json|toml|tf|sh))\b'),
    ("commits", r'(?:sha|commit|"sha")["\s:]*([0-9a-f]{7,40})\b'),
    ("k8s_namespaces", r'(?:-n|--namespace)[=\s]+([a-z][a-z0-9-]*)'),
    ("k8s_namespaces", r'"namespace":\s*"([^"]+)"'),
    ("k8s_pods", r'(?:pod|pods)/([a-z][a-z0-9-]+)'),
    ("k8s_deployments", r'(?:deployment|deploy)/([a-z][a-z0-9-]+)'),
    ("aws_resources", r'\b(i-[0-9a-f]{8,17}|sg-[0-9a-f]+|vpc-[0-9a-f]+|subnet-[0-9a-f]+)\b'),
    ("gcp_projects", r'(?:project[=\s/]+|projects/)([a-z][a-z0-9-]{4,28}[a-z0-9])'),
    ("jira_keys", r'\b([A-Z]{2,10}-\d+)\b'),
    ("services", r'"(?:service|serviceName)":\s*"([^"]+)"'),
]

_RESOURCE_LABELS = {
    "repositories": "Repositories", "files": "Files", "commits": "Commits",
    "k8s_namespaces": "K8s Namespaces", "k8s_pods": "K8s Pods",
    "k8s_deployments": "K8s Deployments", "aws_region": "AWS Region",
    "aws_arns": "AWS ARNs", "aws_resources": "AWS Resources",
    "gcp_projects": "GCP Projects", "endpoints": "Endpoints",
    "jira_keys": "Jira Issues", "services": "Services",
}


def _scan_aws_resources(text: str, resources: dict) -> None:
    """Extract AWS-specific resource identifiers from text."""
    for m in re.finditer(r'(?:--region|us-(?:east|west)-[12]|eu-(?:west|central)-[123]|ap-(?:southeast|northeast|south)-[12])', text):
        region = m.group(0).replace("--region", "").strip()
        if region:
            resources["aws_region"].add(region)

    for m in re.finditer(r'arn:aws:[a-z0-9-]+:[a-z0-9-]*:\d{12}:[^\s"]+', text):
        arn = m.group(0).rstrip(",.")
        if "*" not in arn and ":assumed-role/" not in arn:
            resources["aws_arns"].add(arn)

    for m in re.finditer(r'([a-z][a-z0-9-]+\.(?:[a-z0-9]+\.)?(?:use[12]|usw[12]|euw[123])\.cache\.amazonaws\.com)', text):
        resources["endpoints"].add(m.group(1))
    for m in re.finditer(r'([a-z][a-z0-9-]+\.[a-z0-9]+\.[a-z]{2}-[a-z]+-\d\.rds\.amazonaws\.com)', text):
        resources["endpoints"].add(m.group(1))


def _scan_repos(c: Citation, text: str, accessed_repos: set) -> None:
    """Extract repository references from a citation."""
    tool = c.tool_name or ""
    if any(t in tool for t in _REPO_ACCESS_TOOLS):
        for m in re.finditer(r'"owner":\s*"([^"]+)".*?"repo":\s*"([^"]+)"', text):
            accessed_repos.add(f"{m.group(1)}/{m.group(2)}")
        for m in re.finditer(r'"repository":\s*"([a-zA-Z0-9_-]+/[a-zA-Z0-9._-]+)"', text):
            accessed_repos.add(m.group(1))

    cmd = c.command or ""
    for m in re.finditer(r'raw\.githubusercontent\.com/([a-zA-Z0-9_-]+/[a-zA-Z0-9._-]+)', cmd):
        accessed_repos.add(m.group(1))


def _scan_citation_resources(c: Citation, resources: dict, accessed_repos: set) -> None:
    """Extract resource identifiers from a single citation."""
    text = (c.command or "") + " " + (c.output or "")[:2000]

    _scan_repos(c, text, accessed_repos)

    for key, pattern in _RESOURCE_PATTERNS:
        for m in re.finditer(pattern, text):
            val = m.group(1)[:12] if key == "commits" else m.group(1)
            resources[key].add(val)

    _scan_aws_resources(text, resources)


def _extract_resource_inventory(citations: List[Citation]) -> str:
    """Extract concrete resource identifiers from citation commands and outputs."""
    resources = {k: set() for k in _RESOURCE_LABELS}
    accessed_repos: set = set()

    for c in citations:
        _scan_citation_resources(c, resources, accessed_repos)

    resources["repositories"] = accessed_repos

    lines = []
    for key, label in _RESOURCE_LABELS.items():
        vals = resources[key]
        if vals:
            lines.append(f"  {label}: {', '.join(sorted(vals)[:10])}")

    if not lines:
        return ""
    return "RESOURCE INVENTORY (real values from investigation — use these in commands):\n" + "\n".join(lines)


def _format_citation_line(citation: Citation, output_cap: int) -> str:
    """Format a single citation into a trace line with truncated output."""
    output = citation.output[:output_cap] if citation.output else "(no output)"
    if len(citation.output or "") > output_cap:
        output += "..."
    tool_display = _TOOL_NAME_MAPPING.get(citation.tool_name, citation.tool_name)
    return f"[{citation.index}] {tool_display}: {citation.command}\n    → {output}"


def _build_trace_context(
    citations: List[Citation],
    agent_reasoning: str,
) -> str:
    """Build the investigation trace from citations and agent reasoning."""
    parts = []

    if agent_reasoning:
        reasoning = _INTERNAL_REF_RE.sub("", agent_reasoning)
        if len(reasoning) > _MAX_REASONING_CHARS:
            reasoning = "...[earlier reasoning truncated]\n\n" + reasoning[-_MAX_REASONING_CHARS:]
        parts.append(f"AGENT REASONING (investigator's analysis):\n{reasoning}")

    if citations:
        parts.append("\nINVESTIGATION EVIDENCE (tool calls in chronological order):")
        recent_start = max(0, len(citations) - 5)
        for i, c in enumerate(citations):
            tool_display = _TOOL_NAME_MAPPING.get(c.tool_name, c.tool_name)
            if tool_display in _CODE_TOOLS:
                cap = _CODE_OUTPUT_CHARS
            elif i >= recent_start:
                cap = _RECENT_OUTPUT_CHARS
            else:
                cap = _OLDER_OUTPUT_CHARS
            parts.append(_format_citation_line(c, cap))

    trace = "\n\n".join(parts)

    if len(trace) > _MAX_TRACE_CHARS:
        trace = "...[earlier trace truncated]\n\n" + trace[-_MAX_TRACE_CHARS:]

    return trace


# ---------------------------------------------------------------------------
# Hypothesis extraction — parse structured state from agent reasoning
# ---------------------------------------------------------------------------

def _extract_hypotheses(agent_reasoning: str) -> InvestigationState:
    """Extract hypothesis ledger from agent reasoning using an LLM call.

    This converts the unstructured agent thoughts into the structured
    InvestigationState that the recommender prompt consumes.
    """
    from chat.backend.agent.llm import ModelConfig
    from chat.backend.agent.providers import create_chat_model

    if not agent_reasoning or len(agent_reasoning.strip()) < 100:
        return InvestigationState()

    # Truncate reasoning for extraction (use less budget than full recommender)
    reasoning_input = agent_reasoning[-4000:] if len(agent_reasoning) > 4000 else agent_reasoning

    prompt = f"""Extract the hypothesis state from this investigation reasoning. Return JSON only.

REASONING:
{reasoning_input}

Return a JSON object with:
- "hypotheses": array of open/confirmed hypotheses, each with:
  - "statement": what it claims
  - "probability": "high" | "medium" | "low"
  - "supporting_evidence": citation numbers as strings, e.g. ["3", "7"]
  - "contradicting_evidence": citation numbers as strings
  - "mitigation_class": "rollback" | "restart" | "scale" | "config_revert" | "code_fix" | "unknown"
  - "status": "confirmed" | "open"
- "ruled_out": array of ruled-out hypotheses (same shape, status="ruled_out")
- "unexplored": array of strings describing paths not investigated and why

If root cause is confirmed, there should be exactly one hypothesis with status="confirmed" and probability="high".
Return ONLY the JSON object."""

    try:
        llm = create_chat_model(ModelConfig.SUGGESTION_MODEL, temperature=0.1)
        response = llm.invoke([HumanMessage(content=prompt)])
        text = _strip_code_fences(str(response.content).strip())

        data = json.loads(text)

        state = InvestigationState()

        for h in data.get("hypotheses", []):
            state.hypotheses.append(Hypothesis(
                statement=h.get("statement", ""),
                probability=h.get("probability", "medium"),
                supporting_evidence=h.get("supporting_evidence", []),
                contradicting_evidence=h.get("contradicting_evidence", []),
                mitigation_class=h.get("mitigation_class", "unknown"),
                status=h.get("status", "open"),
            ))

        for h in data.get("ruled_out", []):
            state.ruled_out.append(Hypothesis(
                statement=h.get("statement", ""),
                probability="low",
                supporting_evidence=h.get("supporting_evidence", []),
                contradicting_evidence=h.get("contradicting_evidence", []),
                mitigation_class=h.get("mitigation_class", "unknown"),
                status="ruled_out",
            ))

        state.unexplored = data.get("unexplored", [])

        logger.info(
            "[Recommender] Extracted hypothesis ledger: %d open, %d ruled_out, %d unexplored",
            len(state.hypotheses), len(state.ruled_out), len(state.unexplored),
        )
        return state

    except Exception as e:
        logger.warning("[Recommender] Hypothesis extraction failed: %s", e)
        return InvestigationState()


def _format_confirmed_hypotheses(hypotheses: List[Hypothesis]) -> List[str]:
    """Format confirmed root cause hypotheses."""
    lines = ["\nCONFIRMED ROOT CAUSE:"]
    for h in hypotheses:
        evidence = ", ".join(f"[{e}]" for e in h.supporting_evidence)
        lines.append(f"  • {h.statement} (evidence: {evidence}, mitigation: {h.mitigation_class})")
    return lines


def _format_open_hypotheses(hypotheses: List[Hypothesis]) -> List[str]:
    """Format open hypotheses with evidence and contradictions."""
    lines = ["\nOPEN HYPOTHESES:"]
    for h in hypotheses:
        evidence = ", ".join(f"[{e}]" for e in h.supporting_evidence)
        contra = ", ".join(f"[{e}]" for e in h.contradicting_evidence)
        line = f"  • [{h.probability}] {h.statement} (supports: {evidence}"
        if contra:
            line += f", contradicts: {contra}"
        line += f", mitigation: {h.mitigation_class})"
        lines.append(line)
    return lines


def _format_hypothesis_ledger(state: InvestigationState) -> str:
    """Format the hypothesis ledger for the recommender prompt."""
    if not state.hypotheses and not state.ruled_out:
        return ""

    parts = ["HYPOTHESIS LEDGER:"]

    confirmed = [h for h in state.hypotheses if h.status == "confirmed"]
    open_h = [h for h in state.hypotheses if h.status == "open"]

    if confirmed:
        parts.extend(_format_confirmed_hypotheses(confirmed))
    if open_h:
        parts.extend(_format_open_hypotheses(open_h))

    if state.ruled_out:
        parts.append("\nRULED OUT:")
        for h in state.ruled_out:
            contra = ", ".join(f"[{e}]" for e in h.contradicting_evidence)
            parts.append(f"  • {h.statement} (killed by: {contra})")

    if state.unexplored:
        parts.append("\nNOT INVESTIGATED:")
        for u in state.unexplored:
            parts.append(f"  • {u}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Extract validated fixes from investigation agent's remediation phase
# ---------------------------------------------------------------------------

_CLI_PREFIXES = ("aws ", "kubectl ", "gcloud ", "az ", "terraform ", "helm ",
                 "curl ", "gh ", "docker ", "sed ", "grep ", "python3 ")


def _parse_validated_metadata(lines: List[str]) -> tuple:
    """Parse metadata lines (Command, Validated by, Risk, Note) from a validated entry."""
    validated_by, risk, undo, note, command = "", "medium", None, "", None
    for line in lines:
        line = line.strip()
        if line.startswith("Command:"):
            command = line[len("Command:"):].strip()
        elif line.startswith("Validated by:"):
            validated_by = line[len("Validated by:"):].strip()
        elif line.startswith("Risk:"):
            risk_undo = line[len("Risk:"):].strip()
            risk_match = re.match(r'(\w+)', risk_undo)
            if risk_match:
                risk = risk_match.group(1).lower()
            undo_match = re.search(r'Undo:\s*(.+)', risk_undo)
            if undo_match:
                undo = undo_match.group(1).strip()
        elif line.startswith("Note:"):
            note = line[len("Note:"):].strip()
    if risk not in _VALID_RISKS:
        risk = "medium"
    return validated_by, risk, undo, note, command


def _parse_validated_entry(entry: str) -> Optional[Suggestion]:
    """Parse a single numbered entry from the VALIDATED FIXES block."""
    lines = entry.strip().split('\n')
    if not lines:
        return None

    first_line = lines[0].strip()
    title_match = re.match(r'(.+?):\s*(.+)', first_line)
    if not title_match:
        return None

    title = title_match.group(1).strip()
    title = re.sub(r'^\[(.+)\]$', r'\1', title)
    title = re.sub(r'\*\*(.+?)\*\*', r'\1', title)
    command_or_desc = title_match.group(2).strip()

    inline_command = command_or_desc if any(command_or_desc.startswith(p) for p in _CLI_PREFIXES) else None
    description = "" if inline_command else command_or_desc

    validated_by, risk, undo, note, metadata_command = _parse_validated_metadata(lines[1:])

    command = inline_command or metadata_command

    if not command and ("handler.py" in title.lower() or "code fix" in description.lower()):
        return None

    rationale = f"Validated: {validated_by}" if validated_by else None

    if not description:
        description = note or validated_by or title
    elif note:
        description = f"{description}\n\n{note}"

    suggestion_type = "remediate" if command else "mitigation"

    return Suggestion(
        title=title[:200],
        description=description,
        type=suggestion_type,
        risk=risk,
        command=command,
        rationale=rationale,
        undo=undo,
    )


def _extract_validated_fixes(agent_reasoning: str) -> List[Suggestion]:
    """Parse VALIDATED FIXES block from the investigation agent's final output."""
    if not agent_reasoning or "VALIDATED FIXES:" not in agent_reasoning:
        return []

    start = agent_reasoning.find("VALIDATED FIXES:")
    if start == -1:
        return []

    section = agent_reasoning[start:]
    for marker in _SECTION_END_MARKERS:
        end = section.find(marker, 20)
        if end > 0:
            section = section[:end]
            break

    entry_splits = re.split(r'\n\d+\.\s+', section)
    raw_entries = entry_splits[1:] if len(entry_splits) > 1 else []

    if not raw_entries:
        return []

    suggestions = [s for entry in raw_entries if (s := _parse_validated_entry(entry))]

    logger.info("[Recommender] Parsed %d validated fixes from investigation agent", len(suggestions))
    return suggestions


# ---------------------------------------------------------------------------
# Self-execution round — run safe diagnostics before suggesting
# ---------------------------------------------------------------------------

def _parse_json_list_response(content: str) -> list:
    """Parse an LLM response expected to be a JSON list, stripping code fences."""
    text = content.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        end_index = -1 if lines[-1].strip() == "```" else len(lines)
        text = "\n".join(lines[1:end_index]).strip()
    result = json.loads(text)
    return result if isinstance(result, list) else []


def _self_execute_safe_diagnostics(
    citations: List[Citation],
    user_id: str,
    session_id: str,
) -> List[dict]:
    """Execute safe diagnostic commands that the recommender would otherwise suggest.

    Returns a list of execution results that get folded into the recommender's
    context so it can use the new evidence when generating suggestions.

    Only runs commands that are:
    - Read-only (risk=safe)
    - Executable with Aurora's existing tooling
    - Not already run during the investigation
    """
    from chat.backend.agent.llm import ModelConfig
    from chat.backend.agent.providers import create_chat_model

    # Build a compact list of what was already run
    executed_commands = set()
    for c in citations:
        if c.command:
            executed_commands.add(c.command.strip().lower())

    # Ask the LLM what safe diagnostics it would run if it could
    trace_summary = []
    for c in citations[-10:]:
        tool_display = _TOOL_NAME_MAPPING.get(c.tool_name, c.tool_name)
        output_preview = (c.output or "")[:100]
        trace_summary.append(f"[{c.index}] {tool_display}: {c.command} → {output_preview}")

    prompt = f"""You are reviewing an investigation's evidence to identify safe read-only diagnostic commands that would add useful context. The investigation already ran these commands:

{chr(10).join(trace_summary)}

What additional SAFE, READ-ONLY commands would help clarify the situation? These must be:
- Purely observational (kubectl get, aws logs, curl, gcloud describe, etc.)
- Executable without elevated privileges
- Not duplicates of what was already run
- Likely to produce actionable evidence

Return a JSON array of objects with:
- "command": the exact command to run
- "provider": "aws" | "gcp" | "azure" | "kubectl" | "general"
- "rationale": one sentence on what this would reveal

Return [] if the investigation is already thorough. Max {_MAX_SELF_EXEC_CALLS} commands.
Return ONLY the JSON array."""

    try:
        llm = create_chat_model(ModelConfig.SUGGESTION_MODEL, temperature=0.1)
        response = llm.invoke([HumanMessage(content=prompt)])
        commands_to_run = _parse_json_list_response(str(response.content))
    except Exception as e:
        logger.warning("[Recommender] Self-exec planning failed: %s", e)
        return []

    results = []
    for cmd_spec in commands_to_run[:_MAX_SELF_EXEC_CALLS]:
        command = cmd_spec.get("command", "").strip()
        if not command or not is_command_safe(command):
            continue
        if command.strip().lower() in executed_commands:
            continue
        results.append(_execute_single_diagnostic(cmd_spec, user_id, session_id))

    return results


def _execute_single_diagnostic(cmd_spec: dict, user_id: str, session_id: str) -> dict:
    """Execute a single diagnostic command and return the result."""
    from chat.backend.agent.tools.cloud_exec_tool import cloud_exec

    command = cmd_spec["command"].strip()
    provider = cmd_spec.get("provider", "general")

    try:
        result_json = cloud_exec(
            provider=provider,
            command=command,
            user_id=user_id,
            session_id=session_id,
            timeout=_SELF_EXEC_TIMEOUT,
        )
        result = json.loads(result_json) if isinstance(result_json, str) else result_json
        output = result.get("stdout", result.get("output", ""))[:500]
        success = result.get("success", result.get("returncode", 1) == 0)
        logger.info("[Recommender] Self-executed: %s → %s", command[:80], "ok" if success else "failed")
        return {
            "command": command,
            "provider": provider,
            "output": output,
            "success": success,
            "rationale": cmd_spec.get("rationale", ""),
        }
    except Exception as e:
        logger.warning("[Recommender] Self-exec failed for '%s': %s", command[:80], e)
        return {
            "command": command,
            "provider": provider,
            "output": f"Error: {e}",
            "success": False,
            "rationale": cmd_spec.get("rationale", ""),
        }


def _format_self_exec_results(results: List[dict]) -> str:
    """Format self-execution results for inclusion in the recommender prompt."""
    if not results:
        return ""

    parts = ["\nADDITIONAL DIAGNOSTICS (Aurora ran these just now):"]
    for r in results:
        status = "✓" if r["success"] else "✗"
        parts.append(f"  {status} {r['command']}")
        if r["output"]:
            # Indent output
            for line in r["output"].split("\n")[:5]:
                parts.append(f"      {line}")
            if r["output"].count("\n") > 5:
                parts.append("      ...")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Recommender prompt — Three-Class Invariant
# ---------------------------------------------------------------------------

def _build_recommender_prompt(
    service: str,
    alert_title: str,
    severity: str,
    trace_context: str,
    hypothesis_ledger: str,
    self_exec_results: str,
    resource_inventory: str = "",
    rca_summary: str = "",
) -> str:
    """Build the recommendation prompt following the Three-Class Invariant."""

    summary_section = ""
    if rca_summary:
        truncated = rca_summary[:3000]
        summary_section = f"""
ROOT CAUSE IS CONFIRMED — the investigation already determined the cause:
{truncated}

Because root cause is confirmed:
- Do NOT generate diagnostic suggestions. The diagnosis is complete.
- Generate ONLY mitigations and remediations that directly fix this root cause.
- SIMPLEST FIX FIRST: What is the minimum change to restore service RIGHT NOW? Lead with that. The ideal architecture fix can come second.
"""

    return f"""You are an SRE generating the next actions an engineer should take after an incident investigation.

INCIDENT: {alert_title}
SERVICE: {service} | SEVERITY: {severity}
{summary_section}
{trace_context}

{hypothesis_ledger}
{self_exec_results}
{resource_inventory}

THE THREE-CLASS INVARIANT:
Aurora already executed every safe, automated diagnostic it could. The suggestions you generate must fall into exactly one of three classes:

1. PRIVILEGE GAP — Aurora lacks the tooling, access, or credentials to do this. The engineer has access Aurora doesn't.
   Example: "Check Cloudflare dashboard — no connector available"

2. NON-TRIVIAL RISK — Requires human judgment or authorization before execution. The action could make things worse if the diagnosis is wrong.
   Example: "Roll back deploy v2.3.1 (undo: redeploy v2.3.1)"

3. HUMAN KNOWLEDGE — Requires organizational context, a decision between tradeoffs, or contacting a specific person.
   Example: "Contact jsmith (authored suspect change abc123, 4h before alert)"

If a suggestion doesn't fit one of these three classes, DON'T generate it — Aurora should have done it already.

ACTION-EQUIVALENCE RULE:
For each pair of leading hypotheses, determine whether they require the same or different immediate action. Only generate diagnostic steps that discriminate between hypotheses requiring DIFFERENT mitigations. If all leading hypotheses share the same mitigation, suggest that mitigation directly — do not waste time diagnosing which specific cause is active.

CONSTRAINTS:
- If root cause is confirmed (see CONFIRMED ROOT CAUSE above), you MUST NOT generate any diagnostic suggestions. Go straight to the fix. This is a hard rule.
- If all hypotheses lead to the same fix, just suggest that fix
- If the alert is invalid/phantom (no real service or resource), return []
- No project management (tickets, status updates, notifications) — technical actions only
- Medium/high risk items MUST have an "undo" command showing how to reverse the action
- 1-4 suggestions. Fewer is better. Never pad. Every suggestion must move the incident closer to resolution.
- STRONG PREFERENCE for suggestions with runnable commands. Only omit the command for class "human_knowledge" (decisions, conversations, org context). If you can't write a command for a diagnostic/mitigation/remediate suggestion, drop it.
- For commands: use kubectl, aws, gcloud, az, terraform, helm, curl, jq, gh, docker, sed, grep, python3 (available in sandbox)
- Commands MUST use real values from the RESOURCE INVENTORY above — NEVER use placeholders like <bucket-name> or <timestamp>. If the inventory doesn't contain the value you need, set command to null.
- CAUSATION CHECK: Trace the path from command → system state change → incident resolution. If any link is broken (command succeeds but nothing changes, or change happens but doesn't fix the incident, or the change depends on something outside the engineer's control like a third party receiving an email), DROP the suggestion. Only include suggestions where you can explain the full causal chain from "engineer runs this" to "problem is fixed or meaningfully advanced."
- ROLLBACK RULE: Never suggest a rollback/revert unless the RESOURCE INVENTORY contains evidence that a previous version actually exists (e.g., a list of published Lambda versions, a kubectl rollout history, a deploy log showing a prior version). If the only version is $LATEST or there's a single revision, rollback is impossible — suggest fix-forward instead.
- NO INVESTIGATION-AS-A-FIX: If your "fix" command only LISTS or DESCRIBES something (list-versions, describe-instances, get-function-configuration), that's a diagnostic, not a fix. Either provide the actual mutation command or don't include it.
- TYPE COMPATIBILITY: If the fix requires pointing service A at resource B, verify from the RESOURCE INVENTORY that B has the correct type/engine. Never point a PostgreSQL client at a MySQL endpoint, a gRPC service at an HTTP endpoint, etc. If no compatible resource exists, the fix is "create one" — not "use the wrong one."
- CAUSAL RELEVANCE: Only suggest fixes in the causal chain of THIS incident. If the investigation discovered an unrelated misconfiguration (e.g., a missing event source mapping that isn't causing the reported symptoms), do NOT include it. Every suggestion must connect to the root cause stated in the summary.
- Prefer commands the engineer can paste and run immediately. Include --region, --namespace, --output flags as needed.
- Do NOT use type "fix" — code fixes are generated during investigation with full file access. Use "remediate" for code/config changes and describe what to change in the description.
- Do NOT suggest "prevent" unless it's a concrete one-liner (e.g., adding a CI step). Vague "add monitoring" or "add validation" is useless.

Return a JSON array where each item has:
- "title": action verb + specific target. Use backticks for code terms.
- "description": why this helps + how to verify success. Use backticks for code terms, file paths, values.
- "type": {'"mitigation" | "remediate" | "prevent"' if rca_summary else '"mitigation" | "diagnostic" | "remediate" | "prevent"'}
- "risk": "safe" | "low" | "medium" | "high"
- "command": exact CLI command. Must be directly executable. For code changes, use sed/patch or describe the edit.
- "rationale": one sentence tying this to specific evidence. Include citation numbers like [15].
- "undo": reversal command for medium/high risk, null otherwise
{'''- "expected_outcome": (optional, for diagnostics) object with:
  - "if_true": what it means if the command confirms the hypothesis
  - "if_false": what it means if it doesn't
  - "then": what action to take based on the result
''' if not rca_summary else ''}
Return ONLY the JSON array."""


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _extract_commands_from_trace(citations: List[Citation]) -> set:
    """Extract commands already run during investigation for redundancy filtering."""
    commands = set()
    for c in citations:
        if c.command:
            normalized = c.command.strip().lower()
            commands.add(normalized)
    return commands


def _overlaps_existing_fix(suggestion: Suggestion, fix_files: set) -> bool:
    """Check if a suggestion is redundant with an existing github_fix suggestion."""
    if suggestion.type not in ("remediate", "fix"):
        return False
    title_lower = suggestion.title.lower()
    for fp in fix_files:
        basename = fp.split("/")[-1] if "/" in fp else fp
        if basename and basename in title_lower:
            return True
        if fp and fp in title_lower:
            return True
    return False


def _is_redundant(suggestion: Suggestion, executed_commands: set) -> bool:
    """Check if a suggestion duplicates something already executed."""
    if not suggestion.command:
        return False
    normalized = suggestion.command.strip().lower()
    for executed in executed_commands:
        if normalized == executed:
            return True
        if len(normalized) > 20 and len(executed) > 20:
            shorter = min(normalized, executed, key=len)
            prefix_len = int(len(shorter) * 0.8)
            if normalized[:prefix_len] == executed[:prefix_len]:
                return True
    return False


def _extract_text_part(part: Any) -> str:
    """Extract text from a single content block, filtering out thinking blocks."""
    if isinstance(part, str):
        return part
    if isinstance(part, dict) and part.get("type") not in ("thinking", "reasoning"):
        text = part.get("text", "")
        return str(text) if text else ""
    return ""


def _extract_text_from_content(content: Any) -> str:
    """Extract plain text from LLM response content (handles Gemini thinking blocks)."""
    if isinstance(content, list):
        return "".join(_extract_text_part(part) for part in content).strip()
    return str(content).strip()


def _parse_item_to_suggestion(item: dict) -> Optional[Suggestion]:
    """Convert a single parsed JSON item into a validated Suggestion."""
    if not isinstance(item, dict):
        return None
    title = item.get("title", "").strip()
    if not title:
        return None

    stype = item.get("type", "diagnostic")
    if stype == "fix":
        stype = "remediate"
    if stype not in _VALID_TYPES:
        stype = "diagnostic"
    risk = item.get("risk", "safe")
    if risk not in _VALID_RISKS:
        risk = "safe"

    command = item.get("command")
    if command and re.search(r'<[a-zA-Z][a-zA-Z0-9_-]*>', command):
        command = None
    elif command and not is_command_safe(command):
        risk = "high"

    undo = item.get("undo") if risk in ("medium", "high") else None

    rationale = _build_rationale(item)

    return Suggestion(
        title=title,
        description=item.get("description", "").strip(),
        type=stype,
        risk=risk,
        command=command,
        rationale=rationale,
        undo=undo,
    )


def _build_rationale(item: dict) -> str:
    """Build rationale string including expected_outcome if present."""
    rationale = item.get("rationale", "")
    expected_outcome = item.get("expected_outcome")
    if not expected_outcome or not isinstance(expected_outcome, dict):
        return rationale
    outcome_parts = []
    if expected_outcome.get("if_true"):
        outcome_parts.append(f"If confirmed: {expected_outcome['if_true']}")
    if expected_outcome.get("if_false"):
        outcome_parts.append(f"If not: {expected_outcome['if_false']}")
    if expected_outcome.get("then"):
        outcome_parts.append(f"Then: {expected_outcome['then']}")
    if not outcome_parts:
        return rationale
    joined = " / ".join(outcome_parts)
    return f"{rationale} | {joined}" if rationale else joined


def _parse_recommendations(content: Any, executed_commands: set) -> List[Suggestion]:
    """Parse LLM response into validated, ordered Suggestion objects."""
    if not content:
        return []

    text = _strip_code_fences(_extract_text_from_content(content))

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            fixed = re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", text)
            data = json.loads(fixed)
        except json.JSONDecodeError:
            logger.exception("[Recommender] JSON parse failed. Content: %s", text[:200])
            return []

    if not isinstance(data, list):
        data = [data]

    suggestions = []
    for item in data:
        suggestion = _parse_item_to_suggestion(item)
        if not suggestion:
            continue
        if _is_redundant(suggestion, executed_commands):
            continue
        suggestions.append(suggestion)

    suggestions.sort(key=lambda s: _TYPE_SORT_ORDER.get(s.type, 99))
    return suggestions


# ---------------------------------------------------------------------------
# Enrich validated fixes with LLM-generated descriptions and summaries
# ---------------------------------------------------------------------------

def _enrich_validated_fixes(
    suggestions: List[Suggestion],
    agent_reasoning: str,
    user_id: str,
    session_id: str,
) -> None:
    """Generate proper descriptions and summaries for validated fixes.

    The parser extracts titles and commands but descriptions are often just
    raw validation evidence. This uses a cheap model to write user-facing
    descriptions explaining WHY each fix matters, using the investigation
    context.
    """
    if not suggestions:
        return

    from chat.backend.agent.providers import create_chat_model
    from chat.backend.agent.utils.llm_usage_tracker import tracked_invoke

    items = []
    for i, s in enumerate(suggestions):
        entry = f"{i+1}. TITLE: {s.title}"
        if s.command:
            entry += f"\n   COMMAND: {s.command}"
        if s.rationale:
            entry += f"\n   EVIDENCE: {s.rationale}"
        items.append(entry)

    # Truncate agent reasoning to fit context
    reasoning_excerpt = agent_reasoning[:4000] if agent_reasoning else ""

    prompt = f"""You are writing descriptions for incident remediation steps. For each suggestion below, write:
- A DESCRIPTION (1-2 sentences): what the problem is and why this fix resolves it. Reference specific evidence from the investigation.
- A SUMMARY (max 15 words): the "why" that isn't obvious from the title.

Investigation context (excerpt):
{reasoning_excerpt}

Suggestions:
{chr(10).join(items)}

Return in this exact format (one block per suggestion):
1. DESCRIPTION: <text>
   SUMMARY: <text>
2. DESCRIPTION: <text>
   SUMMARY: <text>"""

    try:
        llm = create_chat_model(_ENRICHMENT_MODEL, temperature=0)
        response = tracked_invoke(
            llm,
            [HumanMessage(content=prompt)],
            user_id=user_id,
            session_id=session_id or None,
            model_name=_ENRICHMENT_MODEL,
            request_type="suggestion_enrichment",
        )
        _apply_enrichment_response(str(response.content).strip(), suggestions)
    except Exception as e:
        logger.warning("[Recommender] Validated fix enrichment failed (non-fatal): %s", e)
        _generate_summaries(suggestions, user_id, session_id)


def _apply_enrichment_response(text: str, suggestions: List[Suggestion]) -> None:
    """Parse enrichment LLM response and apply descriptions/summaries to suggestions."""
    blocks = re.split(r'\n(?=\d+\.)', text)
    for block in blocks:
        m = re.match(r'(\d+)\.\s*DESCRIPTION:\s*(.+?)(?:\n\s*SUMMARY:\s*(.+))?$', block, re.DOTALL)
        if not m:
            continue
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(suggestions):
            desc = m.group(2).strip()
            summary = m.group(3).strip() if m.group(3) else None
            if desc:
                suggestions[idx].description = desc
            if summary:
                suggestions[idx].summary = summary


# ---------------------------------------------------------------------------
# Summary generation (cheap model, one-line per suggestion)
# ---------------------------------------------------------------------------

def _generate_summaries(suggestions: List[Suggestion], user_id: str, session_id: str) -> None:
    """Generate concise one-line summaries for each suggestion using a cheap model."""
    if not suggestions:
        return

    from chat.backend.agent.providers import create_chat_model
    from chat.backend.agent.utils.llm_usage_tracker import tracked_invoke

    items = []
    for i, s in enumerate(suggestions):
        full_text = s.description
        if s.rationale:
            full_text += " " + s.rationale
        items.append(f"{i+1}. TITLE: {s.title}\n   DETAIL: {full_text}")

    prompt = f"""For each suggestion, write a ONE-LINE explanation of the mechanism or root cause — the "why" that isn't obvious from the title. Max 15 words. Do NOT repeat the action from the title.

Good examples (title → summary):
- "Fix batch_writer.go race condition" → "slice[:0] reuse shares backing array across concurrent workers"
- "Re-process corrupted S3 batches" → "$4,200/day revenue gap from duplicate event_ids since v2.15.0 deploy"
- "Block deploys when CI fails" → "race condition shipped because ADS-7 test failures were bypassed"

{chr(10).join(items)}

Return one summary per line, numbered:"""

    try:
        llm = create_chat_model(_ENRICHMENT_MODEL, temperature=0)
        response = tracked_invoke(
            llm,
            [HumanMessage(content=prompt)],
            user_id=user_id,
            session_id=session_id or None,
            model_name=_ENRICHMENT_MODEL,
            request_type="suggestion_summary",
        )
        text = str(response.content).strip()
        lines = [l.strip() for l in text.split("\n") if l.strip()]

        for line in lines:
            m = re.match(r"^(\d+)\.\s*(.+)$", line)
            if m:
                idx = int(m.group(1)) - 1
                if 0 <= idx < len(suggestions):
                    suggestions[idx].summary = m.group(2)
    except Exception as e:
        logger.warning("[Recommender] Summary generation failed (non-fatal): %s", e)


# ---------------------------------------------------------------------------
# Post-generation validation — cross-check commands against resource inventory
# ---------------------------------------------------------------------------

def _validate_table_name(suggestion: Suggestion, inventory_lower: str, resource_inventory: str) -> None:
    """Fix DynamoDB table names in commands that don't match inventory."""
    table_match = re.search(r'--table-name\s+(\S+)', suggestion.command)
    if not table_match:
        return
    table_name = table_match.group(1).strip("'\"")
    if table_name.lower() in inventory_lower:
        return
    suggestion.rationale = (suggestion.rationale or "") + " [WARNING: table name not confirmed in investigation evidence]"


def _should_drop_suggestion(suggestion: Suggestion, resource_inventory: str) -> bool:
    """Return True if the suggestion should be dropped due to failed validation."""
    cmd = suggestion.command
    if not cmd:
        return False
    rollback_words = ('rollback', 'roll-back', 'previous-version', 'revert')
    if any(word in cmd.lower() for word in rollback_words):
        if '$LATEST' not in resource_inventory and 'Version' not in resource_inventory:
            return True
    return False


def _validate_suggestion_commands(
    suggestions: List[Suggestion],
    resource_inventory: str,
) -> List[Suggestion]:
    """Validate commands in suggestions against known resource names."""
    if not suggestions:
        return suggestions

    inventory_lower = resource_inventory.lower()

    validated = []
    for s in suggestions:
        if _should_drop_suggestion(s, resource_inventory):
            continue
        if s.command:
            _validate_table_name(s, inventory_lower, resource_inventory)
        validated.append(s)

    if len(validated) < len(suggestions):
        logger.info("[Recommender:Validate] Dropped %d suggestions that failed validation", len(suggestions) - len(validated))

    return validated


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_recommendations(
    incident_id: str,
    citations: List[Citation],
    agent_reasoning: str,
    service: str,
    alert_title: str,
    severity: str = "unknown",
    user_id: str = "",
    session_id: str = "",
    existing_fixes: Optional[List[dict]] = None,
    rca_summary: str = "",
) -> List[Suggestion]:
    """Generate next steps from the investigation trace.

    Pipeline:
    1. Extract hypothesis ledger from agent reasoning
    2. Run self-execution round (safe diagnostics)
    3. Generate suggestions for human-required actions only
    4. Dedup against existing fix suggestions from github_fix tool
    """
    from chat.backend.agent.llm import ModelConfig
    from chat.backend.agent.providers import create_chat_model
    from chat.backend.agent.utils.llm_usage_tracker import tracked_invoke

    # Step 0: Check if the investigation agent already validated fixes
    validated_fixes = _extract_validated_fixes(agent_reasoning)
    if validated_fixes:
        logger.info("[Recommender] Found %d pre-validated fixes from investigation agent", len(validated_fixes))
        _enrich_validated_fixes(validated_fixes, agent_reasoning, user_id, session_id)
        return validated_fixes

    # Step 1: Extract hypothesis ledger
    hypothesis_state = _extract_hypotheses(agent_reasoning)
    hypothesis_ledger = _format_hypothesis_ledger(hypothesis_state)

    # Step 2: Self-execution round — run safe diagnostics before suggesting
    self_exec_results_raw = []
    if user_id and session_id:
        try:
            self_exec_results_raw = _self_execute_safe_diagnostics(
                citations=citations,
                user_id=user_id,
                session_id=session_id,
            )
        except Exception as e:
            logger.warning("[Recommender] Self-execution round failed: %s", e)

    self_exec_context = _format_self_exec_results(self_exec_results_raw)

    # Step 3: Build trace and generate suggestions
    trace_context = _build_trace_context(citations, agent_reasoning)
    executed_commands = _extract_commands_from_trace(citations)
    resource_inventory = _extract_resource_inventory(citations)

    # Also add self-executed commands to the "already done" set
    for r in self_exec_results_raw:
        if r.get("success") and r.get("command"):
            executed_commands.add(r["command"].strip().lower())

    # Build context about existing fix suggestions (from github_fix tool)
    fix_context = ""
    if existing_fixes:
        fix_lines = [f"  - {f.get('file_path', '?')}: {f.get('title', '')[:80]}" for f in existing_fixes]
        fix_context = "EXISTING CODE FIXES (already generated with full diffs — do NOT duplicate these):\n" + "\n".join(fix_lines)

    prompt = _build_recommender_prompt(
        service=service,
        alert_title=alert_title,
        severity=severity,
        trace_context=trace_context,
        hypothesis_ledger=hypothesis_ledger,
        self_exec_results=self_exec_context,
        resource_inventory=resource_inventory + ("\n\n" + fix_context if fix_context else ""),
        rca_summary=rca_summary,
    )

    try:
        llm = create_chat_model(ModelConfig.SUGGESTION_MODEL, temperature=0.3)
        response = tracked_invoke(
            llm,
            [HumanMessage(content=prompt)],
            user_id=user_id,
            session_id=session_id or None,
            model_name=ModelConfig.SUGGESTION_MODEL,
            request_type="recommendation",
        )
        suggestions = _parse_recommendations(response.content, executed_commands)
        suggestions = _post_process_suggestions(
            suggestions, existing_fixes, resource_inventory, user_id, session_id,
        )

        logger.info(
            "[Recommender] Generated %d recommendations for incident %s "
            "(hypotheses: %d confirmed, %d open | self-exec: %d commands)",
            len(suggestions), incident_id,
            len([h for h in hypothesis_state.hypotheses if h.status == "confirmed"]),
            len([h for h in hypothesis_state.hypotheses if h.status == "open"]),
            len(self_exec_results_raw),
        )
        return suggestions

    except Exception as e:
        logger.exception("[Recommender] Failed for incident %s: %s", incident_id, e)
        return []


def _post_process_suggestions(
    suggestions: List[Suggestion],
    existing_fixes: Optional[List[dict]],
    resource_inventory: str,
    user_id: str,
    session_id: str,
) -> List[Suggestion]:
    """Dedup, validate, and enrich suggestions after LLM generation."""
    if existing_fixes:
        fix_files = {f.get("file_path", "").lower().strip() for f in existing_fixes if f.get("file_path")}
        suggestions = [s for s in suggestions if not _overlaps_existing_fix(s, fix_files)]

    if resource_inventory:
        suggestions = _validate_suggestion_commands(suggestions, resource_inventory)

    _generate_summaries(suggestions, user_id, session_id)
    return suggestions


