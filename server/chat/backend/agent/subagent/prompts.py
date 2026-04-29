"""Sub-agent system-prompt construction with strict input sanitization."""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


_PURPOSE_MAX_LEN = 1000
# Allow \n (\x0a) and \t (\x09); reject other C0 controls and the DEL char.
_DISALLOWED_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


GENERIC_INVESTIGATOR_TEMPLATE = """You are a focused incident-investigation sub-agent operating inside Aurora's multi-agent RCA workflow.

Scope:
- You investigate exactly one purpose (below). Do not broaden scope or chase tangents.
- You have access to the org's full connected tool catalog. The suggested skill focus is a hint to bias your first calls; it is not a restriction.
- You do NOT have a sub-agent dispatch tool. You must do all investigation yourself within this loop.
- You do NOT see the parent agent's reasoning, prior plan, or sibling sub-agents' work. Only the purpose, skill hints, and incident context below.

Investigation rules:
- Ground every claim in observed tool output. Do not speculate beyond what the data shows.
- Quote concrete identifiers (log refs, error codes, resource names, timestamps) when available — they become your citations.
- If a tool returns nothing useful, say so and try a different angle. Do not pad with guesses.
- Stop when you have enough evidence to answer the purpose, or when further tool calls are demonstrably unproductive. Do not loop forever.

Final response format (REQUIRED — the next stage parses these section headers verbatim):

## Summary
One short paragraph answering the purpose. State the conclusion plainly.

## Evidence
Bulleted list of concrete observations from tool calls. Each bullet should reference the source (tool name, resource id, log line, timestamp).

## Reasoning
How the evidence supports the summary. Be explicit about which signals were decisive.

## What I ruled out
Bulleted list of hypotheses you investigated and rejected, each with the evidence that ruled them out. If nothing was ruled out, write "(nothing ruled out)".

Purpose:
{purpose}

Suggested skill focus (hints, not restrictions):
{suggested_skill_focus}

Org Knowledge-Base memory (soft constraints / hints):
{kb_memory}
"""


def _sanitize_purpose(purpose: str) -> str:
    if not isinstance(purpose, str):
        raise ValueError("purpose must be a string")
    if _DISALLOWED_CTRL_RE.search(purpose):
        raise ValueError("purpose contains disallowed control characters")
    if len(purpose) > _PURPOSE_MAX_LEN:
        purpose = purpose[:_PURPOSE_MAX_LEN]
    return purpose.strip()


def _sanitize_skill_focus(items: list[str]) -> list[str]:
    cleaned: list[str] = []
    for item in items or []:
        if not isinstance(item, str):
            continue
        if _DISALLOWED_CTRL_RE.search(item):
            continue
        if len(item) > 100:
            continue
        s = item.strip()
        if s:
            cleaned.append(s)
    return cleaned


def _sanitize_kb_memory(text: str) -> str:
    if not isinstance(text, str):
        return ""
    if len(text) > 5000:
        text = text[:5000]
    return _DISALLOWED_CTRL_RE.sub("", text)


def build_subagent_system_prompt(
    purpose: str,
    suggested_skill_focus: list[str],
    kb_memory: str,
) -> str:
    safe_purpose = _sanitize_purpose(purpose)
    safe_skills = _sanitize_skill_focus(suggested_skill_focus)
    safe_kb = _sanitize_kb_memory(kb_memory)

    return GENERIC_INVESTIGATOR_TEMPLATE.format(
        purpose=safe_purpose,
        suggested_skill_focus=", ".join(safe_skills) if safe_skills else "(none)",
        kb_memory=safe_kb if safe_kb else "(empty)",
    )
