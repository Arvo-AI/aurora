"""Agentic mini-RCA (Layer 2) for Startup Check.

Layer 1 (deterministic rules in ``rules.py``) is the trust anchor: it parses
Kubernetes manifests and drives the SAFE/RISKY verdict. Layer 2 is advisory
reasoning on top of the SAME fetch: a bounded agent reads the raw file
contents the scanner already pulled (Docker Compose, CI, plain config — not
just the manifests Layer 1 understands) and reasons about incident
preconditions the static rules cannot see.

Advisory means three hard guarantees:

- observations are tagged ``source="agent"`` and NEVER flip the verdict;
- the whole pass fails closed — any model/tool error yields an empty result
  and the deterministic verdict stands alone; and
- it is bounded: a fixed step budget, per-file truncation, and a tool-output
  cap keep one scan from becoming an open-ended RCA crawl.

Model strategy mirrors the fact interpreter: premium-first with a free
fallback. ``STARTUP_CHECK_AGENT_MODEL`` set -> exactly that model, no
fallback.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field  # type: ignore

from services.startup_check.context import UserContext

logger = logging.getLogger(__name__)

# Bounded reasoning budget — keeps a scan inside one fetch, not an RCA crawl.
MAX_STEPS = 5               # tool-call rounds before we synthesize
MAX_LINES_PER_FILE = 150    # per-file truncation for read_file
MAX_TOOL_OUTPUT = 8 * 1024  # cap on any single tool response (bytes)

FREE_FALLBACK_MODEL = "nvidia/nemotron-3.5-lightning:free"

_VALID_SEVERITIES = {"HIGH", "MEDIUM", "LOW"}

_SYSTEM_PROMPT = """You are the agentic layer of a repository Startup Check — a read-only,
pre-incident review. You reason about a repository's deployment files to
surface the preconditions of production incidents BEFORE they reach a cluster.

You are given a map of file paths plus three read-only tools:
- list_files: list every available path
- read_file: read one file's raw content (bounded/truncated)
- search: find lines matching a substring or regex across all files

Unlike the deterministic checks (which only understand Kubernetes manifests),
you can read ANY file in the map: Docker Compose, CI workflows, config, and
scripts. Investigate specific files with the tools — never assert content you
have not actually read. Prefer the files most likely to hold deploy/run logic.

Rules:
- Flag ONLY concrete, file-evidenced risks that could cause a production
  incident: crashes, outages, resource exhaustion, misrouting, secrets in
  plaintext, broken healthchecks, port mismatches, unsafe defaults.
- Be conservative — low false positives. Ambiguous files are not findings.
- Do NOT repeat findings the deterministic Kubernetes rules already cover;
  focus on what they miss (non-K8s files, cross-file logic, Docker/CI).
- Each observation must cite an exact file_path (and line when known) that
  exists in the map. If nothing warrants a flag, return zero observations."""

_FINAL_INSTRUCTION = (
    "Now produce your structured analysis from what you read: a one-to-two "
    "sentence summary plus a list of observations (empty if nothing to flag)."
)


def _model_chain() -> List[str]:
    """Ordered models for the agent pass, premium first."""
    explicit = os.getenv("STARTUP_CHECK_AGENT_MODEL")
    if explicit:
        return [explicit]
    try:
        from chat.backend.agent.llm import ModelConfig

        premium = ModelConfig.RCA_MODEL
    except Exception:  # noqa: BLE001 — config import must never break scans
        logger.warning("[STARTUP-CHECK] ModelConfig unavailable; free model only")
        return [FREE_FALLBACK_MODEL]
    return [premium, FREE_FALLBACK_MODEL]


class _Observation(BaseModel):
    severity: str = "LOW"
    file_path: str = ""
    line: Optional[int] = None
    title: str = ""
    explanation: str = ""


class AgentAnalysis(BaseModel):
    """Structured-output contract for the mini-RCA pass."""

    summary: str = ""
    observations: List[_Observation] = Field(default_factory=list)


def _truncate_file(content: str, path: str) -> str:
    """Bound one file for the read_file tool: line cap + byte cap."""
    lines = content.splitlines()
    total = len(lines)
    if total > MAX_LINES_PER_FILE:
        lines = lines[:MAX_LINES_PER_FILE]
    text = "\n".join(lines)
    if len(text) > MAX_TOOL_OUTPUT:
        text = text[:MAX_TOOL_OUTPUT]
    if total > MAX_LINES_PER_FILE:
        text += f"\n... [{path}: {total} lines, showing first {MAX_LINES_PER_FILE}]"
    return text


def _list_files_text(files: Dict[str, str]) -> str:
    return "\n".join(sorted(files)) if files else "(no files available)"


def _read_file_text(files: Dict[str, str], path: str) -> str:
    content = files.get(path)
    if content is None:
        return f"(file not found: {path})"
    return _truncate_file(content, path)


def _search_text(files: Dict[str, str], pattern: str) -> str:
    try:
        regex = re.compile(pattern)
    except re.error:
        regex = re.compile(re.escape(pattern))
    hits: List[str] = []
    total = 0
    for path in sorted(files):
        for lineno, line in enumerate(files[path].splitlines(), start=1):
            if regex.search(line):
                snippet = f"{path}:{lineno}: {line.strip()[:200]}"
                if total + len(snippet) > MAX_TOOL_OUTPUT:
                    hits.append("... (more matches, truncated)")
                    return "\n".join(hits)
                hits.append(snippet)
                total += len(snippet)
    return "\n".join(hits) if hits else "(no matches)"


def _make_tools(files: Dict[str, str]) -> List[StructuredTool]:
    """Read-only tools scoped to the fetched files (lazy: map, not dump)."""

    def list_files() -> str:
        """List every file path available to this scan, one per line."""
        return _list_files_text(files)

    def read_file(path: str) -> str:
        """Return the raw contents of one file, truncated to a bounded size.

        Args:
            path: the file path (as returned by list_files).
        """
        return _read_file_text(files, path)

    def search(pattern: str) -> str:
        """Search all file contents for a substring or regex.

        Args:
            pattern: a literal substring or Python regex to search for.
        """
        return _search_text(files, pattern)

    return [
        StructuredTool.from_function(func=list_files, name="list_files", description="List available file paths."),
        StructuredTool.from_function(func=read_file, name="read_file", description="Read one file's raw content (bounded)."),
        StructuredTool.from_function(func=search, name="search", description="Search file contents for a substring/regex."),
    ]


def _context_facts(context: Optional[UserContext]) -> Dict[str, Any]:
    if context is None:
        return {}
    return {
        "nodes": context.nodes,
        "service_memory_mb": context.service_memory_mb,
        "storage_classes": context.storage_classes,
        "default_storage_class": context.default_storage_class,
        "cluster_domain": context.cluster_domain,
    }


def _build_task_prompt(
    repo_full_name: str, context: Optional[UserContext], files: Dict[str, str]
) -> str:
    """The agent's map: file paths + environment facts, never file bodies."""
    lines = [f"Repository: {repo_full_name}", "", f"Available files ({len(files)}):"]
    lines += [f"  - {p}" for p in sorted(files)]
    facts = {k: v for k, v in _context_facts(context).items() if v}
    if facts:
        lines += ["", "Environment facts the user provided (ground truth — do not invent others):"]
        for key, value in facts.items():
            lines.append(f"  - {key}: {json.dumps(value)}")
    return "\n".join(lines)


def _to_findings(analysis: AgentAnalysis, files: Dict[str, str]) -> List[Dict[str, Any]]:
    """Validate + tag the model's observations as source="agent" findings."""
    findings: List[Dict[str, Any]] = []
    for i, obs in enumerate(analysis.observations):
        severity = (obs.severity or "").strip().upper()
        if severity not in _VALID_SEVERITIES:
            continue
        file_path = (obs.file_path or "").strip()
        if file_path not in files:
            continue  # never surface a hallucinated path
        title = (obs.title or "").strip()
        explanation = (obs.explanation or "").strip()
        if not title and not explanation:
            continue
        findings.append({
            "id": f"agent:{i}",
            "rule": "agent",
            "severity": severity,
            "source": "agent",
            "file_path": file_path,
            "line": obs.line,
            "end_line": obs.line,
            "title": title or "Agent observation",
            "explanation": explanation,
        })
    return findings


def _to_result(
    analysis: AgentAnalysis, files: Dict[str, str], model_name: str
) -> Dict[str, Any]:
    findings = _to_findings(analysis, files)
    summary = (analysis.summary or "").strip()
    if not summary:
        summary = (
            f"Agent reviewed {len(files)} files and flagged {len(findings)} concern(s)."
            if findings
            else f"Agent reviewed {len(files)} files and found no additional concerns."
        )
    return {
        "agent_used": True,
        "agent_model": model_name,
        "agent_summary": summary,
        "agent_observations": findings,
        "agent_error": None,
    }


def _empty_result(reason: str) -> Dict[str, Any]:
    return {
        "agent_used": False,
        "agent_model": None,
        "agent_summary": "",
        "agent_observations": [],
        "agent_error": reason,
    }


def _track_usage(
    user_id: str, prompt: str, raw_response: Any, start_time: float, model_name: str
) -> None:
    """Best-effort usage tracking — never fails the scan."""
    try:
        from chat.backend.agent.utils.llm_usage_tracker import LLMUsageTracker

        LLMUsageTracker.track_llm_call(
            user_id=user_id,
            session_id=None,
            model_name=model_name,
            request_type="startup_check_agent",
            prompt=prompt,
            response=raw_response,
            start_time=start_time,
            api_provider=os.getenv("LLM_PROVIDER_MODE", "direct"),
        )
    except Exception:  # noqa: BLE001 — tracking must never break the scan
        logger.info("[STARTUP-CHECK] agent usage tracking failed", exc_info=True)


def _run_with_model(
    user_id: str,
    model_name: str,
    tools: List[StructuredTool],
    task_prompt: str,
    files: Dict[str, str],
) -> Optional[Dict[str, Any]]:
    """One model's tool loop + structured synthesis. Returns a result dict or
    None when the model produced unparsable output (caller tries next)."""
    from chat.backend.agent.providers import create_chat_model

    model = create_chat_model(model_name, temperature=0, streaming=False)
    tool_model = model.bind_tools(tools)
    tools_by_name = {t.name: t for t in tools}

    messages = [SystemMessage(content=_SYSTEM_PROMPT), HumanMessage(content=task_prompt)]
    for _ in range(MAX_STEPS):
        resp = tool_model.invoke(messages)
        messages.append(resp)
        tool_calls = getattr(resp, "tool_calls", None) or []
        if not tool_calls:
            break
        for tc in tool_calls:
            tool = tools_by_name.get(tc.get("name"))
            if tool is None:
                output = f"(unknown tool: {tc.get('name')})"
            else:
                try:
                    output = str(tool.invoke(tc.get("args") or {}))
                except Exception as exc:  # noqa: BLE001 — one bad tool call is not fatal
                    output = f"(tool error: {exc})"
            messages.append(ToolMessage(content=output, tool_call_id=tc.get("id", "")))

    structured = model.with_structured_output(
        AgentAnalysis, include_raw=True, method="function_calling"
    )
    start = time.time()
    final = structured.invoke(messages + [HumanMessage(content=_FINAL_INSTRUCTION)])
    parsed = final.get("parsed")
    if parsed is None:
        return None
    _track_usage(user_id, task_prompt, final.get("raw"), start, model_name)
    return _to_result(parsed, files, model_name)


def run_agent_analysis(
    user_id: str,
    repo_full_name: str,
    files: Dict[str, str],
    context: Optional[UserContext],
) -> Dict[str, Any]:
    """Advisory mini-RCA over the fetched files. Fails closed to empty."""
    if not files:
        return _empty_result("no files fetched to analyze")
    tools = _make_tools(files)
    task_prompt = _build_task_prompt(repo_full_name, context, files)

    for model_name in _model_chain():
        try:
            result = _run_with_model(user_id, model_name, tools, task_prompt, files)
        except Exception as exc:  # noqa: BLE001 — one bad model must not end the chain
            logger.warning(
                "[STARTUP-CHECK] agent model %s failed (%s); trying next",
                model_name, str(exc)[:200],
            )
            continue
        if result is not None:
            return result
        logger.warning(
            "[STARTUP-CHECK] agent model %s returned unparsable output; trying next",
            model_name,
        )
    return _empty_result("agent models unavailable")
