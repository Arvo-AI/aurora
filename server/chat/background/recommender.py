"""Next Steps v3: generate suggestions from the investigation trace, not the summary.

The Recommender consumes raw investigation evidence (tool calls + outputs + agent
reasoning) and optionally executes safe read-only diagnostics before generating
suggestions that actually require a human.
"""

import json
import logging
import re
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

_VALID_TYPES = frozenset({"mitigation", "diagnostic", "remediate", "prevent", "fix"})
_VALID_RISKS = frozenset({"safe", "low", "medium", "high"})

_TYPE_SORT_ORDER = {"mitigation": 0, "diagnostic": 1, "remediate": 2, "prevent": 3}


def _friendly_tool_name(raw_name: str) -> str:
    """Map internal tool names to display-friendly names."""
    return _TOOL_NAME_MAPPING.get(raw_name, raw_name)


_INTERNAL_REF_RE = re.compile(
    r"(?:suggestion\s+(?:ID|id)[:\s]*\d+|RCA-\d+|tool\s+call\s*\[\d+\])", re.IGNORECASE
)


def _build_trace_context(
    citations: List[Citation],
    agent_reasoning: str,
) -> str:
    """Build the investigation trace from citations and agent reasoning.

    Structure: agent reasoning first (the "why"), then chronological evidence.
    Recent tool outputs get more space since they're closest to the conclusion.
    """
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
        # Last 5 citations get more output space (most relevant to conclusion)
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


def _build_recommender_prompt(
    service: str,
    alert_title: str,
    severity: str,
    trace_context: str,
    tools_available: List[str],
) -> str:
    """Build the recommendation prompt that consumes the raw trace."""
    tools_note = ""
    if tools_available:
        tools_note = (
            f"\nTOOLS AURORA HAS ACCESS TO: {', '.join(tools_available)}\n"
            "Do NOT suggest read-only actions using these tools. Aurora already ran them "
            "or could run them. Only suggest actions requiring human judgment, "
            "elevated privileges, or tooling Aurora lacks.\n"
        )

    return f"""You are an SRE generating the next actions an engineer should take after an incident investigation.

INCIDENT: {alert_title}
SERVICE: {service} | SEVERITY: {severity}
{tools_note}
INVESTIGATION TRACE:
{trace_context}

Based on what the investigation found, what should the engineer DO next? Think like an experienced SRE who just finished reading the investigation and is handing off to the person who will fix it.

Constraints:
- Only suggest actions the investigation didn't already do
- If root cause is confirmed, skip diagnostics — go straight to the fix
- If all hypotheses lead to the same fix, just suggest that fix
- If the alert is invalid/phantom (no real service or resource), return []
- No project management (tickets, status updates, notifications) — technical actions only
- Medium/high risk items need an "undo" command
- 1-5 suggestions. Fewer is better. Never pad.

EXECUTION ENVIRONMENT:
Commands run in a sandboxed container with: kubectl, aws, gcloud, az, terraform, helm, curl, jq, python3.
Git is NOT available — for code changes, use type "fix" (Aurora applies via GitHub API).
Diagnostic commands (kubectl get, aws logs, curl) can be executed directly.
Multi-step git workflows (clone, edit, commit, push, PR) should be a single "fix" type suggestion with the file path and description of the change — Aurora handles the rest.

Return a JSON array where each item has:
- "title": what to do (action verb + specific target). Use backticks around code terms.
- "description": why this helps + how to verify it worked. Use backticks around code terms (function names, config keys, file paths, CLI commands, values).
- "type": "mitigation" | "diagnostic" | "remediate" | "prevent" | "fix"
  - Use "fix" when the action is a code/config change in a repository. Provide file_path and change_description instead of command.
- "risk": "safe" | "low" | "medium" | "high"
- "command": exact CLI command for diagnostic/mitigation/remediate/prevent types. Must be executable in the sandbox (kubectl, aws, curl, terraform, helm, etc.). null for "fix" type.
- "file_path": (fix type only) path to the file to change, e.g. "cache/redis.go"
- "change_description": (fix type only) what to change in the file
- "rationale": one sentence on what evidence supports this. Include citation numbers like [15] if referencing investigation evidence.
- "undo": reversal command for medium/high risk, null otherwise

Return ONLY the JSON array."""


def _extract_commands_from_trace(citations: List[Citation]) -> set:
    """Extract commands already run during investigation for redundancy filtering."""
    commands = set()
    for c in citations:
        if c.command:
            normalized = c.command.strip().lower()
            commands.add(normalized)
    return commands


def _is_redundant(suggestion: Suggestion, executed_commands: set) -> bool:
    """Check if a suggestion duplicates something already executed."""
    if not suggestion.command:
        return False
    normalized = suggestion.command.strip().lower()
    for executed in executed_commands:
        if normalized == executed:
            return True
        # Fuzzy: if the core command (minus flags/args differences) matches
        if len(normalized) > 20 and len(executed) > 20:
            # Compare first 80% of the shorter string
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

        # Validate and clamp type/risk to allowed values
        stype = item.get("type", "diagnostic")
        if stype not in _VALID_TYPES:
            stype = "diagnostic"
        risk = item.get("risk", "safe")
        if risk not in _VALID_RISKS:
            risk = "safe"

        command = item.get("command")
        if command and not is_command_safe(command):
            logger.warning("[Recommender] Dangerous command flagged: %s", command[:100])
            risk = "high"

        undo = item.get("undo")
        # Enforce: undo required for medium/high, null for safe/low
        if risk in ("medium", "high") and not undo:
            logger.debug("[Recommender] Missing undo for %s-risk suggestion: %s", risk, title)
        if risk in ("safe", "low"):
            undo = None

        suggestion = Suggestion(
            title=title,
            description=item.get("description", "").strip(),
            type=stype,
            risk=risk,
            command=command,
            rationale=item.get("rationale"),
            undo=undo,
        )

        # Filter redundant suggestions
        if _is_redundant(suggestion, executed_commands):
            logger.info("[Recommender] Filtered redundant suggestion: %s", title)
            continue

        suggestions.append(suggestion)

    # Sort: mitigation → diagnostic → remediate → prevent → communication
    suggestions.sort(key=lambda s: _TYPE_SORT_ORDER.get(s.type, 99))

    return suggestions


def _get_available_tool_names(citations: List[Citation]) -> List[str]:
    """Extract unique display-friendly tool names from the investigation."""
    seen = set()
    names = []
    for c in citations:
        display = _friendly_tool_name(c.tool_name)
        if display not in seen:
            seen.add(display)
            names.append(display)
    return names


def generate_recommendations(
    incident_id: str,
    citations: List[Citation],
    agent_reasoning: str,
    service: str,
    alert_title: str,
    severity: str = "unknown",
    user_id: str = "",
    session_id: str = "",
) -> List[Suggestion]:
    """Generate next steps from the investigation trace.

    This is the v3 replacement for SuggestionExtractor.extract_suggestions().
    It consumes the raw trace (citations + reasoning) rather than the prose summary.
    """
    from chat.backend.agent.llm import ModelConfig
    from chat.backend.agent.providers import create_chat_model
    from chat.backend.agent.utils.llm_usage_tracker import tracked_invoke

    trace_context = _build_trace_context(citations, agent_reasoning)
    tools_available = _get_available_tool_names(citations)
    executed_commands = _extract_commands_from_trace(citations)

    prompt = _build_recommender_prompt(
        service=service,
        alert_title=alert_title,
        severity=severity,
        trace_context=trace_context,
        tools_available=tools_available,
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
        logger.info(
            "[Recommender] Generated %d recommendations for incident %s",
            len(suggestions), incident_id,
        )
        return suggestions

    except Exception as e:
        logger.exception("[Recommender] Failed for incident %s: %s", incident_id, e)
        return []
