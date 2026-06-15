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
import re
from dataclasses import dataclass, field
from typing import Any, List, Optional

from langchain_core.messages import HumanMessage

from chat.background.citation_extractor import Citation
from chat.background.suggestion_extractor import Suggestion, is_command_safe

try:
    from chat.background.citation_extractor import _TOOL_NAME_MAPPING
except ImportError:
    _TOOL_NAME_MAPPING = {}

logger = logging.getLogger(__name__)

_MAX_TRACE_CHARS = 30_000
_MAX_REASONING_CHARS = 6_000
_RECENT_OUTPUT_CHARS = 600
_OLDER_OUTPUT_CHARS = 200

_VALID_TYPES = frozenset({"mitigation", "diagnostic", "remediate", "prevent"})
_VALID_RISKS = frozenset({"safe", "low", "medium", "high"})

_TYPE_SORT_ORDER = {"mitigation": 0, "diagnostic": 1, "remediate": 2, "prevent": 3}

# Max self-execution round tool calls
_MAX_SELF_EXEC_CALLS = 5
_SELF_EXEC_TIMEOUT = 30  # seconds per command


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

def _friendly_tool_name(raw_name: str) -> str:
    """Map internal tool names to display-friendly names."""
    return _TOOL_NAME_MAPPING.get(raw_name, raw_name)


_INTERNAL_REF_RE = re.compile(
    r"(?:suggestion\s+(?:ID|id)[:\s]*\d+|RCA-\d+|tool\s+call\s*\[\d+\])", re.IGNORECASE
)


def _extract_resource_inventory(citations: List[Citation]) -> str:
    """Extract concrete resource identifiers from citation commands and outputs.

    Scans all citations for real values (namespaces, pod names, ARNs, repos,
    file paths, commit SHAs, endpoints, etc.) and returns a formatted inventory
    the recommender can reference when building commands.
    """
    resources: dict[str, set] = {
        "repositories": set(),
        "files": set(),
        "commits": set(),
        "k8s_namespaces": set(),
        "k8s_pods": set(),
        "k8s_deployments": set(),
        "aws_region": set(),
        "aws_arns": set(),
        "aws_resources": set(),
        "gcp_projects": set(),
        "endpoints": set(),
        "jira_keys": set(),
        "services": set(),
    }

    # Track which repos were actively accessed (not just search results)
    accessed_repos = set()
    repo_access_tools = {"MCP: Get File Contents", "MCP: Get Commit", "MCP: List Commits",
                         "MCP: Create Or Update File", "GitHub RCA"}

    for c in citations:
        text = (c.command or "") + " " + (c.output or "")[:2000]
        tool = c.tool_name or ""

        # GitHub repos — only from tools that actually accessed content
        if any(t in tool for t in repo_access_tools):
            for m in re.finditer(r'"owner":\s*"([^"]+)".*?"repo":\s*"([^"]+)"', text):
                accessed_repos.add(f"{m.group(1)}/{m.group(2)}")
            for m in re.finditer(r'"repository":\s*"([a-zA-Z0-9_-]+/[a-zA-Z0-9._-]+)"', text):
                accessed_repos.add(m.group(1))
        # curl to raw.githubusercontent — only from the command part
        cmd = c.command or ""
        for m in re.finditer(r'raw\.githubusercontent\.com/([a-zA-Z0-9_-]+/[a-zA-Z0-9._-]+)', cmd):
            accessed_repos.add(m.group(1))

        # File paths from code (*.go, *.py, *.ts, *.yaml, etc.)
        for m in re.finditer(r'(?:^|[\s"/])([a-zA-Z][\w./]*\.(?:go|py|ts|js|yaml|yml|json|toml|tf|sh))\b', text):
            resources["files"].add(m.group(1))

        # Git commit SHAs (7+ hex chars, exclude things that look like AWS resource IDs)
        for m in re.finditer(r'(?:sha|commit|"sha")["\s:]*([0-9a-f]{7,40})\b', text):
            resources["commits"].add(m.group(1)[:12])

        # Kubernetes namespaces
        for m in re.finditer(r'(?:-n|--namespace)[=\s]+([a-z][a-z0-9-]*)', text):
            resources["k8s_namespaces"].add(m.group(1))
        for m in re.finditer(r'"namespace":\s*"([^"]+)"', text):
            resources["k8s_namespaces"].add(m.group(1))

        # Kubernetes pods/deployments
        for m in re.finditer(r'(?:pod|pods)/([a-z][a-z0-9-]+)', text):
            resources["k8s_pods"].add(m.group(1))
        for m in re.finditer(r'(?:deployment|deploy)/([a-z][a-z0-9-]+)', text):
            resources["k8s_deployments"].add(m.group(1))

        # AWS region
        for m in re.finditer(r'(?:--region|us-(?:east|west)-[12]|eu-(?:west|central)-[123]|ap-(?:southeast|northeast|south)-[12])', text):
            region = m.group(0).replace("--region", "").strip()
            if region:
                resources["aws_region"].add(region)

        # AWS ARNs (exclude wildcard and assumed-role session ARNs)
        for m in re.finditer(r'arn:aws:[a-z0-9-]+:[a-z0-9-]*:\d{12}:[^\s"]+', text):
            arn = m.group(0).rstrip(",.")
            if "*" not in arn and ":assumed-role/" not in arn:
                resources["aws_arns"].add(arn)

        # AWS resource identifiers (cluster IDs, instance IDs, etc.)
        for m in re.finditer(r'\b(i-[0-9a-f]{8,17}|sg-[0-9a-f]+|vpc-[0-9a-f]+|subnet-[0-9a-f]+)\b', text):
            resources["aws_resources"].add(m.group(1))

        # ElastiCache / RDS endpoints
        for m in re.finditer(r'([a-z][a-z0-9-]+\.(?:[a-z0-9]+\.)?(?:use[12]|usw[12]|euw[123])\.cache\.amazonaws\.com)', text):
            resources["endpoints"].add(m.group(1))
        for m in re.finditer(r'([a-z][a-z0-9-]+\.[a-z0-9]+\.[a-z]{2}-[a-z]+-\d\.rds\.amazonaws\.com)', text):
            resources["endpoints"].add(m.group(1))

        # GCP projects
        for m in re.finditer(r'(?:project[=\s/]+|projects/)([a-z][a-z0-9-]{4,28}[a-z0-9])', text):
            resources["gcp_projects"].add(m.group(1))

        # Jira issue keys
        for m in re.finditer(r'\b([A-Z]{2,10}-\d+)\b', text):
            resources["jira_keys"].add(m.group(1))

        # Service names from tool outputs
        for m in re.finditer(r'"(?:service|serviceName)":\s*"([^"]+)"', text):
            resources["services"].add(m.group(1))

    resources["repositories"] = accessed_repos

    # Format non-empty categories
    lines = []
    labels = {
        "repositories": "Repositories",
        "files": "Files",
        "commits": "Commits",
        "k8s_namespaces": "K8s Namespaces",
        "k8s_pods": "K8s Pods",
        "k8s_deployments": "K8s Deployments",
        "aws_region": "AWS Region",
        "aws_arns": "AWS ARNs",
        "aws_resources": "AWS Resources",
        "gcp_projects": "GCP Projects",
        "endpoints": "Endpoints",
        "jira_keys": "Jira Issues",
        "services": "Services",
    }
    for key, label in labels.items():
        vals = resources[key]
        if vals:
            # Limit to 10 per category to keep prompt manageable
            items = sorted(vals)[:10]
            lines.append(f"  {label}: {', '.join(items)}")

    if not lines:
        return ""

    return "RESOURCE INVENTORY (real values from investigation — use these in commands):\n" + "\n".join(lines)


def _build_trace_context(
    citations: List[Citation],
    agent_reasoning: str,
) -> str:
    """Build the investigation trace from citations and agent reasoning."""
    parts = []

    if agent_reasoning:
        reasoning = _INTERNAL_REF_RE.sub("", agent_reasoning)
        if len(reasoning) > _MAX_REASONING_CHARS:
            reasoning = reasoning[-_MAX_REASONING_CHARS:]
            reasoning = "...[earlier reasoning truncated]\n\n" + reasoning
        parts.append(f"AGENT REASONING (investigator's analysis):\n{reasoning}")

    if citations:
        parts.append("\nINVESTIGATION EVIDENCE (tool calls in chronological order):")
        n = len(citations)
        recent_start = max(0, n - 5)

        for i, c in enumerate(citations):
            cap = _RECENT_OUTPUT_CHARS if i >= recent_start else _OLDER_OUTPUT_CHARS
            output = c.output[:cap] if c.output else "(no output)"
            if len(c.output or "") > cap:
                output += "..."
            tool_display = _friendly_tool_name(c.tool_name)
            parts.append(f"[{c.index}] {tool_display}: {c.command}\n    → {output}")

    trace = "\n\n".join(parts)

    if len(trace) > _MAX_TRACE_CHARS:
        trace = trace[-_MAX_TRACE_CHARS:]
        trace = "...[earlier trace truncated]\n\n" + trace

    return trace


# ---------------------------------------------------------------------------
# Hypothesis extraction — parse structured state from agent reasoning
# ---------------------------------------------------------------------------

def _extract_hypotheses(agent_reasoning: str, citations: List[Citation]) -> InvestigationState:
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
        text = str(response.content).strip()

        if text.startswith("```"):
            lines = text.split("\n")
            end_index = -1 if lines[-1].strip() == "```" else len(lines)
            text = "\n".join(lines[1:end_index]).strip()

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


def _format_hypothesis_ledger(state: InvestigationState) -> str:
    """Format the hypothesis ledger for the recommender prompt."""
    if not state.hypotheses and not state.ruled_out:
        return ""

    parts = ["HYPOTHESIS LEDGER:"]

    confirmed = [h for h in state.hypotheses if h.status == "confirmed"]
    open_h = [h for h in state.hypotheses if h.status == "open"]

    if confirmed:
        parts.append("\nCONFIRMED ROOT CAUSE:")
        for h in confirmed:
            evidence = ", ".join(f"[{e}]" for e in h.supporting_evidence)
            parts.append(f"  • {h.statement} (evidence: {evidence}, mitigation: {h.mitigation_class})")

    if open_h:
        parts.append("\nOPEN HYPOTHESES:")
        for h in open_h:
            evidence = ", ".join(f"[{e}]" for e in h.supporting_evidence)
            contra = ", ".join(f"[{e}]" for e in h.contradicting_evidence)
            line = f"  • [{h.probability}] {h.statement} (supports: {evidence}"
            if contra:
                line += f", contradicts: {contra}"
            line += f", mitigation: {h.mitigation_class})"
            parts.append(line)

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
# Self-execution round — run safe diagnostics before suggesting
# ---------------------------------------------------------------------------

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
        tool_display = _friendly_tool_name(c.tool_name)
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
        text = str(response.content).strip()

        if text.startswith("```"):
            lines = text.split("\n")
            end_index = -1 if lines[-1].strip() == "```" else len(lines)
            text = "\n".join(lines[1:end_index]).strip()

        commands_to_run = json.loads(text)
        if not isinstance(commands_to_run, list):
            return []

    except Exception as e:
        logger.warning("[Recommender] Self-exec planning failed: %s", e)
        return []

    # Execute each command
    results = []
    for cmd_spec in commands_to_run[:_MAX_SELF_EXEC_CALLS]:
        command = cmd_spec.get("command", "").strip()
        provider = cmd_spec.get("provider", "general")

        if not command:
            continue

        # Safety check — reject anything that looks mutating
        if not is_command_safe(command):
            logger.warning("[Recommender] Self-exec rejected unsafe command: %s", command[:100])
            continue

        # Skip if already executed
        if command.strip().lower() in executed_commands:
            continue

        try:
            from chat.backend.agent.tools.cloud_exec_tool import cloud_exec

            result_json = cloud_exec(
                provider=provider,
                command=command,
                user_id=user_id,
                session_id=session_id,
                timeout=_SELF_EXEC_TIMEOUT,
            )
            result = json.loads(result_json) if isinstance(result_json, str) else result_json
            output = result.get("stdout", result.get("output", ""))[:500]

            results.append({
                "command": command,
                "provider": provider,
                "output": output,
                "success": result.get("success", result.get("returncode", 1) == 0),
                "rationale": cmd_spec.get("rationale", ""),
            })

            logger.info("[Recommender] Self-executed: %s → %s", command[:80], "ok" if results[-1]["success"] else "failed")

        except Exception as e:
            logger.warning("[Recommender] Self-exec failed for '%s': %s", command[:80], e)
            results.append({
                "command": command,
                "provider": provider,
                "output": f"Error: {e}",
                "success": False,
                "rationale": cmd_spec.get("rationale", ""),
            })

    return results


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
) -> str:
    """Build the recommendation prompt following the Three-Class Invariant."""

    return f"""You are an SRE generating the next actions an engineer should take after an incident investigation.

INCIDENT: {alert_title}
SERVICE: {service} | SEVERITY: {severity}

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
- If root cause is confirmed, skip diagnostics — go straight to the fix
- If all hypotheses lead to the same fix, just suggest that fix
- If the alert is invalid/phantom (no real service or resource), return []
- No project management (tickets, status updates, notifications) — technical actions only
- Medium/high risk items MUST have an "undo" command showing how to reverse the action
- 1-4 suggestions. Fewer is better. Never pad. Every suggestion must move the incident closer to resolution.
- STRONG PREFERENCE for suggestions with runnable commands. Only omit the command for class "human_knowledge" (decisions, conversations, org context). If you can't write a command for a diagnostic/mitigation/remediate suggestion, drop it.
- For commands: use kubectl, aws, gcloud, az, terraform, helm, curl, jq, gh, docker, sed, grep, python3 (available in sandbox)
- Commands MUST use real values from the RESOURCE INVENTORY above — NEVER use placeholders like <bucket-name> or <timestamp>. If the inventory doesn't contain the value you need, set command to null.
- Prefer commands the engineer can paste and run immediately. Include --region, --namespace, --output flags as needed.
- Do NOT use type "fix" — code fixes are generated during investigation with full file access. Use "remediate" for code/config changes and describe what to change in the description.
- Do NOT suggest "prevent" unless it's a concrete one-liner (e.g., adding a CI step). Vague "add monitoring" or "add validation" is useless.

Return a JSON array where each item has:
- "title": action verb + specific target. Use backticks for code terms.
- "description": why this helps + how to verify success. Use backticks for code terms, file paths, values.
- "type": "mitigation" | "diagnostic" | "remediate" | "prevent"
- "risk": "safe" | "low" | "medium" | "high"
- "class": "privilege_gap" | "non_trivial_risk" | "human_knowledge" (which invariant class)
- "command": exact CLI command. Must be directly executable. For code changes, use sed/patch or describe the edit.
- "rationale": one sentence tying this to specific evidence. Include citation numbers like [15].
- "undo": reversal command for medium/high risk, null otherwise
- "expected_outcome": (optional, for diagnostics) object with:
  - "if_true": what it means if the command confirms the hypothesis
  - "if_false": what it means if it doesn't
  - "then": what action to take based on the result

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


def _parse_recommendations(content: Any, executed_commands: set) -> List[Suggestion]:
    """Parse LLM response into validated, ordered Suggestion objects."""
    if not content:
        return []

    # Handle Gemini thinking model responses
    if isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") not in ("thinking", "reasoning"):
                    text = part.get("text", "")
                    if text:
                        text_parts.append(str(text))
            elif isinstance(part, str):
                text_parts.append(part)
        text = "".join(text_parts).strip()
    else:
        text = str(content).strip()

    # Strip markdown code fences
    if text.startswith("```"):
        lines = text.split("\n")
        end_index = -1 if lines[-1].strip() == "```" else len(lines)
        text = "\n".join(lines[1:end_index]).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            fixed = re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", text)
            data = json.loads(fixed)
        except json.JSONDecodeError as e:
            logger.error("[Recommender] JSON parse failed: %s. Content: %s", e, text[:200])
            return []

    if not isinstance(data, list):
        data = [data]

    suggestions = []
    for item in data:
        if not isinstance(item, dict):
            continue
        title = item.get("title", "").strip()
        if not title:
            continue

        stype = item.get("type", "diagnostic")
        if stype == "fix":
            stype = "remediate"
        if stype not in _VALID_TYPES:
            stype = "diagnostic"
        risk = item.get("risk", "safe")
        if risk not in _VALID_RISKS:
            risk = "safe"

        command = item.get("command")
        if command:
            if re.search(r'<[a-zA-Z][a-zA-Z0-9_-]*>', command):
                logger.info("[Recommender] Stripped command with placeholders: %s", command[:100])
                command = None
            elif not is_command_safe(command):
                logger.warning("[Recommender] Dangerous command flagged: %s", command[:100])
                risk = "high"

        undo = item.get("undo")
        if risk in ("medium", "high") and not undo:
            logger.debug("[Recommender] Missing undo for %s-risk suggestion: %s", risk, title)
        if risk in ("safe", "low"):
            undo = None

        # Build rationale with expected_outcome if present
        rationale = item.get("rationale", "")
        expected_outcome = item.get("expected_outcome")
        if expected_outcome and isinstance(expected_outcome, dict):
            outcome_parts = []
            if expected_outcome.get("if_true"):
                outcome_parts.append(f"If confirmed: {expected_outcome['if_true']}")
            if expected_outcome.get("if_false"):
                outcome_parts.append(f"If not: {expected_outcome['if_false']}")
            if expected_outcome.get("then"):
                outcome_parts.append(f"Then: {expected_outcome['then']}")
            if outcome_parts:
                rationale = rationale + " | " + " / ".join(outcome_parts) if rationale else " / ".join(outcome_parts)

        suggestion = Suggestion(
            title=title,
            description=item.get("description", "").strip(),
            type=stype,
            risk=risk,
            command=command,
            rationale=rationale,
            undo=undo,
        )

        if _is_redundant(suggestion, executed_commands):
            logger.info("[Recommender] Filtered redundant suggestion: %s", title)
            continue

        suggestions.append(suggestion)

    # Sort: mitigation → diagnostic → fix → remediate → prevent
    suggestions.sort(key=lambda s: _TYPE_SORT_ORDER.get(s.type, 99))

    return suggestions


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
        llm = create_chat_model("anthropic/claude-haiku-4.5", temperature=0)
        response = tracked_invoke(
            llm,
            [HumanMessage(content=prompt)],
            user_id=user_id,
            session_id=session_id or None,
            model_name="anthropic/claude-haiku-4.5",
            request_type="suggestion_summary",
        )
        text = str(response.content).strip()
        lines = [l.strip() for l in text.split("\n") if l.strip()]

        for line in lines:
            import re
            m = re.match(r"^(\d+)\.\s*(.+)$", line)
            if m:
                idx = int(m.group(1)) - 1
                if 0 <= idx < len(suggestions):
                    suggestions[idx].summary = m.group(2)
    except Exception as e:
        logger.warning("[Recommender] Summary generation failed (non-fatal): %s", e)


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

    # Step 1: Extract hypothesis ledger
    hypothesis_state = _extract_hypotheses(agent_reasoning, citations)
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

        # Dedup: remove suggestions that overlap with existing fix suggestions
        if existing_fixes:
            fix_files = {f.get("file_path", "").lower().strip() for f in existing_fixes if f.get("file_path")}
            pre_dedup = len(suggestions)
            suggestions = [
                s for s in suggestions
                if not _overlaps_existing_fix(s, fix_files)
            ]
            if len(suggestions) < pre_dedup:
                logger.info("[Recommender] Deduped %d suggestions covered by existing fixes", pre_dedup - len(suggestions))

        # Generate one-line summaries with a cheap model
        _generate_summaries(suggestions, user_id, session_id)

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
