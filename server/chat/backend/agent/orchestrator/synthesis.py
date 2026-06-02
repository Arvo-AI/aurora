"""Synthesis node: reads all sub-agent findings and produces a unified RCA summary."""

import asyncio
import logging
import time
from typing import Optional

from langchain_core.messages import AIMessage, ToolMessage
from pydantic import BaseModel

from chat.backend.agent.llm import ModelConfig
from chat.backend.agent.providers import create_chat_model
from chat.backend.agent.orchestrator.usage import track_orchestrator_call
from chat.backend.agent.utils.state import State
from chat.backend.agent.orchestrator.inputs import SubAgentInput
from chat.backend.agent.orchestrator.dispatcher import (
    DISPATCH_SUBAGENT_TOOL_NAME,
    dispatch_tool_call_id,
)
from chat.backend.agent.orchestrator.tool_cache import get_cache_hit_count
from chat.backend.agent.orchestrator.triage import _apply_per_role_caps
from utils.log_sanitizer import hash_for_log

logger = logging.getLogger(__name__)

_MAX_SYNTHESIS_WAVES = 2
_MAX_FOLLOWUPS = 6
_MAX_FINDINGS_CHARS = 12000

# Caps for orchestrator-context fields embedded in the synthesis prompt.
# Findings themselves are already capped at _MAX_FINDINGS_CHARS; these are
# kept small enough that the combined prompt comfortably fits inside the model
# window while still surfacing the full alert + plan.
_MAX_ORCH_QUESTION_CHARS = 4000
_MAX_ORCH_ALERT_FIELD_CHARS = 600
_MAX_ORCH_RATIONALE_CHARS = 800
_MAX_ORCH_PURPOSE_CHARS = 500


class SynthesisDecision(BaseModel):
    needs_more_research: bool = False
    follow_up_inputs: list[SubAgentInput] = []
    rationale: str = ""
    summary: str = ""


async def synthesis_node(state: State) -> dict:
    try:
        return await _synthesis(state)
    except Exception:
        logger.exception(
            "synthesis_node: unhandled error for incident %s",
            hash_for_log(getattr(state, "incident_id", "") or ""),
        )
        return {
            "synthesis_wave": (getattr(state, "synthesis_wave", 0) or 0) + 1,
            "subagent_inputs": [],
        }


async def _synthesis(state: State) -> dict:
    incident_id = getattr(state, "incident_id", None) or ""
    user_id = getattr(state, "user_id", None) or ""
    inc_hash = hash_for_log(incident_id)
    current_wave = getattr(state, "synthesis_wave", 0) or 0

    # Synthesize findings from the wave that just completed. Wave column on
    # rca_findings is set by dispatcher to current_wave+1; synthesis sees that
    # wave on this turn before incrementing state.synthesis_wave below.
    target_wave = current_wave + 1

    # Build ToolMessages closing the synthetic dispatch tool_calls round-trip.
    tool_messages = _build_tool_messages(state, incident_id, target_wave)

    # Fetch findings up to and including the wave that just completed. The
    # final summary needs full context across waves; the needs_more decision
    # is steered by which findings are NEW (target_wave) via the prompt.
    finding_rows = await asyncio.to_thread(_fetch_finding_rows, incident_id, user_id, target_wave)
    with_uri = [row for row in finding_rows if row.get("storage_uri")]
    raw_bodies = await asyncio.gather(
        *(asyncio.to_thread(_download_finding, row["storage_uri"], user_id) for row in with_uri),
        return_exceptions=True,
    ) if with_uri else []
    body_by_agent: dict = {}
    for row, body in zip(with_uri, raw_bodies):
        if isinstance(body, BaseException):
            logger.warning(
                "synthesis_node: finding download raised for agent %s: %s",
                row.get("agent_id"), body,
            )
            continue
        if body:
            body_by_agent[row.get("agent_id")] = body

    # Don't drop rows that have status but no body (timed out / failed before
    # upload). Emit a synthetic stanza so the lead sees that the branch ran.
    finding_bodies: list[str] = []
    for row in finding_rows:
        header = f"## Wave {row.get('wave', '?')} | Agent: {row.get('agent_id')} ({row.get('role_name')})"
        purpose = row.get("purpose")
        if purpose:
            header = f"{header}\nAssigned purpose: {_trunc(purpose, _MAX_ORCH_PURPOSE_CHARS, 'finding_purpose')}"
        body = body_by_agent.get(row.get("agent_id"))
        if body:
            finding_bodies.append(f"{header}\n\n{body}")
        else:
            status = row.get("status") or "unknown"
            strength = row.get("self_assessed_strength") or "n/a"
            err = row.get("error_message") or "no findings body uploaded"
            finding_bodies.append(
                f"{header}\n\nstatus: {status}\nstrength: {strength}\nbody unavailable ({err})"
            )

    new_wave = current_wave + 1

    if not finding_bodies:
        logger.warning("synthesis_node: no findings to synthesize for incident %s", inc_hash)
        existing_messages = list(getattr(state, "messages", []) or [])
        fallback_text = (
            "Sub-agents completed but no findings were available to synthesize."
        )
        return {
            "synthesis_wave": new_wave,
            "subagent_inputs": [],
            "messages": existing_messages + tool_messages + [AIMessage(content=fallback_text)],
        }

    combined = "\n\n---\n\n".join(finding_bodies)

    try:
        if not ModelConfig.RCA_ORCHESTRATOR_MODEL:
            raise RuntimeError(
                "RCA_ORCHESTRATOR_MODEL must be set when ORCHESTRATOR_ENABLED=true"
            )
        # Non-streaming: structured-output chunks must not leak into chat.
        # The user-facing summary is appended below as an AIMessage that the
        # existing chat pipeline handles.
        llm = create_chat_model(model=ModelConfig.RCA_ORCHESTRATOR_MODEL, streaming=False)
        structured = llm.with_structured_output(
            SynthesisDecision, include_raw=True, method="function_calling"
        )

        # Pass available role names to constrain follow-up dispatch — prevents
        # the LLM from hallucinating role names that aren't in the registry.
        from chat.backend.agent.orchestrator.role_registry import RoleRegistry
        try:
            available_roles = RoleRegistry.get_instance().list_available_roles(user_id)
        except Exception:
            available_roles = []

        orchestrator_thoughts = _build_orchestrator_thoughts(state)
        prompt = _build_synthesis_prompt(
            state, combined, current_wave, available_roles, orchestrator_thoughts,
        )
        _log_orchestrator_context_injection(
            inc_hash, target_wave, orchestrator_thoughts, prompt, len(finding_bodies),
        )
        start_time = time.time()
        result = await structured.ainvoke(prompt)

        decision = result.get("parsed") or SynthesisDecision(
            needs_more_research=False,
            rationale="synthesis parse error fallback",
            summary="Synthesis parse error; please review the sub-agent findings directly.",
        )
        await asyncio.to_thread(
            track_orchestrator_call,
            state, result.get("raw"), prompt,
            "synthesis_decision", start_time,
        )

        # Defense-in-depth: drop follow-up inputs whose role_name isn't registered.
        # If `available_roles` is empty (registry lookup failed), this drops ALL
        # follow-ups — preferable to dispatching hallucinated roles that fail in wave 2.
        valid_role_names = {r.name for r in available_roles}
        if decision.follow_up_inputs:
            before = len(decision.follow_up_inputs)
            decision.follow_up_inputs = [
                inp for inp in decision.follow_up_inputs if inp.role_name in valid_role_names
            ]
            dropped = before - len(decision.follow_up_inputs)
            if dropped:
                logger.warning(
                    "synthesis_node: dropped %d follow_up_inputs with unknown role names", dropped,
                )

            decision.follow_up_inputs = _apply_per_role_caps(decision.follow_up_inputs)

            if len(decision.follow_up_inputs) > _MAX_FOLLOWUPS:
                logger.warning(
                    "synthesis_node: %d follow_up_inputs exceeds cap %d — truncating",
                    len(decision.follow_up_inputs), _MAX_FOLLOWUPS,
                )
                decision.follow_up_inputs = decision.follow_up_inputs[:_MAX_FOLLOWUPS]

            if not decision.follow_up_inputs:
                decision.needs_more_research = False

        logger.info(
            "synthesis_node: incident=%s wave=%d needs_more=%s follow_ups=%d",
            inc_hash, current_wave, decision.needs_more_research, len(decision.follow_up_inputs),
        )
    except Exception:
        logger.exception("synthesis_node: LLM synthesis failed for incident %s", inc_hash)
        decision = SynthesisDecision(
            needs_more_research=False,
            rationale="synthesis LLM error",
            summary="Synthesis encountered an error; please review the sub-agent findings directly.",
        )

    final_summary_text = (decision.summary or "").strip()
    is_terminal = new_wave >= _MAX_SYNTHESIS_WAVES or not decision.needs_more_research

    if is_terminal:
        if not final_summary_text:
            final_summary_text = (
                "Investigation complete. See sub-agent findings above for details."
            )
    else:
        # Intermediate wave — keep chat alive between waves
        if not final_summary_text:
            final_summary_text = (
                "Initial findings inconclusive — investigating further..."
            )

    existing_messages = list(getattr(state, "messages", []) or [])
    final_ai_msg = AIMessage(content=final_summary_text)
    new_messages = existing_messages + tool_messages + [final_ai_msg]

    # Persist the orchestrator's own decision for this wave so subsequent
    # synthesis waves can reason about what was already considered. This is
    # the "main orchestrator thoughts" that must accompany the sub-agent tool
    # output into later summarizations to prevent hallucinations.
    history_entry = {
        "wave": target_wave,
        "rationale": (decision.rationale or "")[:_MAX_ORCH_RATIONALE_CHARS],
        "needs_more_research": bool(decision.needs_more_research) and not is_terminal,
        "follow_up_inputs": [
            {
                "agent_id": inp.agent_id,
                "role_name": inp.role_name,
                "purpose": (inp.purpose or "")[:_MAX_ORCH_PURPOSE_CHARS],
            }
            for inp in (decision.follow_up_inputs or [])
        ],
    }
    existing_history = list(getattr(state, "synthesis_history", []) or [])
    new_history = existing_history + [history_entry]

    if is_terminal:
        logger.info(
            "synthesis: incident=%s wave=%d cache_hits=%d",
            inc_hash, new_wave, get_cache_hit_count(incident_id),
        )
        return {
            "synthesis_wave": new_wave,
            "subagent_inputs": [],
            "messages": new_messages,
            "synthesis_history": new_history,
        }

    return {
        "synthesis_wave": new_wave,
        "subagent_inputs": [inp.model_dump() for inp in decision.follow_up_inputs],
        "messages": new_messages,
        "synthesis_history": new_history,
    }


def _build_tool_messages(state: State, incident_id: str, target_wave: int) -> list[ToolMessage]:
    """Build one ToolMessage per finding_ref for the wave that just completed.

    ID format MUST match dispatch_tool_call_id used by dispatcher.
    """
    refs = list(getattr(state, "finding_refs", []) or [])
    out: list[ToolMessage] = []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        ref_wave = ref.get("wave")
        # Only attach for the just-completed wave; skip prior-wave refs we already closed.
        if ref_wave is not None and ref_wave != target_wave:
            continue
        agent_id = ref.get("agent_id")
        if not agent_id:
            continue
        tc_id = dispatch_tool_call_id(incident_id, agent_id, target_wave)
        content = (
            ref.get("summary")
            or f"{ref.get('status', 'completed')} ({ref.get('self_assessed_strength') or 'inconclusive'})"
        )
        out.append(ToolMessage(
            content=content,
            tool_call_id=tc_id,
            name=DISPATCH_SUBAGENT_TOOL_NAME,
            additional_kwargs={
                "self_assessed_strength": ref.get("self_assessed_strength"),
            },
        ))
    return out


def _build_synthesis_prompt(state: State, combined_findings: str, wave: int,
                              available_roles: list,
                              orchestrator_thoughts: str) -> str:
    """Build the synthesis prompt.

    Findings are grouped by wave in `combined_findings`. The LLM judges
    `needs_more_research` from what the NEW wave (target_wave = wave+1) added,
    but writes the user-facing `summary` using full context across all waves.

    `orchestrator_thoughts` carries the main orchestrator's grounding context
    (alert details, triage rationale, sub-agent plan, prior wave decisions).
    The summary must remain grounded in findings, but uses this context to
    avoid hallucinating alert facts that the sub-agents did not re-state.
    """
    target_wave = wave + 1
    role_lines = "\n".join(
        f"- {r.name}: {r.description}" for r in available_roles
    ) or "(none available)"
    findings_len = len(combined_findings)
    if findings_len > _MAX_FINDINGS_CHARS:
        logger.info("synthesis: truncated combined_findings from %d to %d chars", findings_len, _MAX_FINDINGS_CHARS)
        combined_findings = combined_findings[:_MAX_FINDINGS_CHARS]

    return (
        f"You are an RCA orchestrator synthesizing parallel investigation findings.\n\n"
        f"Most recent wave: {target_wave} (max {_MAX_SYNTHESIS_WAVES})\n\n"
        f"{orchestrator_thoughts}\n\n"
        f"Available investigator roles for follow-up (use ONLY these role_name values):\n{role_lines}\n\n"
        f"=== SUB-AGENT FINDINGS (grouped by wave) ===\n\n{combined_findings}\n\n"
        f"=== TASK ===\n"
        f"Treat ORCHESTRATOR CONTEXT as ground-truth facts about the alert and "
        f"the investigation plan. Treat SUB-AGENT FINDINGS as the only source "
        f"of evidence for what was actually discovered. Do NOT invent findings, "
        f"metrics, error messages, or root causes that are not present in the "
        f"sub-agent findings; if a sub-agent did not investigate something, say so.\n\n"
        f"1) needs_more_research: judge based on what wave {target_wave} added. "
        f"If the new wave (or, on wave 1, the only wave) provides a clear root cause "
        f"with at least one strong/moderate-confidence finding, return false. "
        f"If critical gaps remain that another wave could fill AND target_wave < {_MAX_SYNTHESIS_WAVES}, "
        f"return true with follow_up_inputs. Each follow_up_input needs agent_id "
        f"(e.g. sa_w{target_wave + 1}_1), role_name (from the list above), purpose.\n"
        f"2) summary: a concise (3-6 sentence) user-facing markdown summary using ALL findings "
        f"across every wave shown above. Cover what was found, the most likely root cause(s), "
        f"and (if needs_more_research=true) what's being investigated next. Shown directly to the user.\n"
        f"3) rationale: brief reasoning for your decision."
    )


def _build_orchestrator_thoughts(state: State) -> str:
    """Render the main orchestrator's grounding context as a prompt block.

    Includes:
    - The original RCA question / synthesized alert prompt header.
    - Structured alert details from rca_context (title, severity, status,
      message, service) — these come from the webhook/trigger payload and
      are the authoritative description of what's being investigated.
    - The triage decision rationale + the sub-agent assignments (agent_id ->
      role + purpose) so the synthesizer can see what each sub-agent was
      tasked with, not just what they returned.
    - Prior wave synthesis rationales when running wave 2+ so the
      orchestrator's previous reasoning is preserved across waves.

    All fields are bounded to keep the prompt within model context.
    """
    incident_id = getattr(state, "incident_id", "unknown") or "unknown"
    question = (getattr(state, "question", "") or "")[:_MAX_ORCH_QUESTION_CHARS]

    lines: list[str] = [
        "=== ORCHESTRATOR CONTEXT (grounding facts; NOT evidence) ===",
        f"Incident: {incident_id}",
    ]

    rca_context = getattr(state, "rca_context", None) or {}
    if isinstance(rca_context, dict):
        alert_block = _format_rca_context(rca_context)
        if alert_block:
            lines.append("")
            lines.append("Alert details (from trigger payload):")
            lines.append(alert_block)

    triage_block = _format_triage_decision(getattr(state, "triage_decision", None))
    if triage_block:
        lines.append("")
        lines.append("Triage decision (main orchestrator reasoning):")
        lines.append(triage_block)

    history_block = _format_synthesis_history(getattr(state, "synthesis_history", None))
    if history_block:
        lines.append("")
        lines.append("Prior synthesis decisions (orchestrator thoughts from earlier waves):")
        lines.append(history_block)

    if question:
        lines.append("")
        lines.append("Original RCA prompt (excerpt):")
        lines.append(question)

    return "\n".join(lines)


def _trunc(value, limit: int, field: str = "") -> str:
    s = "" if value is None else str(value)
    if len(s) <= limit:
        return s
    logger.info("synthesis: truncated %s from %d to %d chars", field or "field", len(s), limit)
    return s[:limit] + "...[truncated]"


def _format_rca_context(rca_context: dict) -> str:
    """Surface the alert facts the orchestrator was given. Pulls only fields
    we know to be safe to embed (no secrets, no large blobs)."""
    fields = [
        ("title", "Title"),
        ("severity", "Severity"),
        ("status", "Status"),
        ("source", "Source"),
        ("service", "Service"),
        ("message", "Message"),
    ]
    parts: list[str] = []
    for key, label in fields:
        val = rca_context.get(key)
        if val:
            parts.append(f"- {label}: {_trunc(val, _MAX_ORCH_ALERT_FIELD_CHARS, label)}")
    providers = rca_context.get("providers")
    if isinstance(providers, (list, tuple)) and providers:
        parts.append(f"- Connected providers: {', '.join(str(p) for p in providers)}")
    return "\n".join(parts)


def _format_triage_decision(triage_decision) -> str:
    """Render the triage rationale + the planned sub-agent assignments."""
    if not triage_decision:
        return ""
    if isinstance(triage_decision, dict):
        mode = triage_decision.get("mode")
        rationale = triage_decision.get("rationale") or ""
        inputs = triage_decision.get("inputs") or []
    else:
        mode = getattr(triage_decision, "mode", None)
        rationale = getattr(triage_decision, "rationale", "") or ""
        inputs = getattr(triage_decision, "inputs", []) or []

    parts: list[str] = []
    if mode:
        parts.append(f"- Mode: {mode}")
    if rationale:
        parts.append(f"- Rationale: {_trunc(rationale, _MAX_ORCH_RATIONALE_CHARS, 'triage_rationale')}")
    if inputs:
        parts.append("- Sub-agents dispatched:")
        for inp in inputs:
            if isinstance(inp, dict):
                agent_id = inp.get("agent_id") or "?"
                role = inp.get("role_name") or "?"
                purpose = inp.get("purpose") or ""
            else:
                agent_id = getattr(inp, "agent_id", "?")
                role = getattr(inp, "role_name", "?")
                purpose = getattr(inp, "purpose", "")
            parts.append(
                f"  - {agent_id} ({role}): {_trunc(purpose, _MAX_ORCH_PURPOSE_CHARS, 'purpose')}"
            )
    return "\n".join(parts)


def _format_synthesis_history(history) -> str:
    if not history:
        return ""
    parts: list[str] = []
    for entry in history:
        if not isinstance(entry, dict):
            continue
        wave = entry.get("wave", "?")
        rationale = _trunc(entry.get("rationale", ""), _MAX_ORCH_RATIONALE_CHARS, "history_rationale")
        needs_more = entry.get("needs_more_research")
        parts.append(f"- Wave {wave}: needs_more_research={bool(needs_more)}")
        if rationale:
            parts.append(f"  rationale: {rationale}")
        follow_ups = entry.get("follow_up_inputs") or []
        for fu in follow_ups:
            if not isinstance(fu, dict):
                continue
            agent_id = fu.get("agent_id") or "?"
            role = fu.get("role_name") or "?"
            purpose = _trunc(fu.get("purpose", ""), _MAX_ORCH_PURPOSE_CHARS, "history_purpose")
            parts.append(f"  follow-up {agent_id} ({role}): {purpose}")
    return "\n".join(parts)


def _log_orchestrator_context_injection(inc_hash: str, target_wave: int,
                                          orchestrator_thoughts: str,
                                          full_prompt: str,
                                          findings_count: int) -> None:
    """Emit an INFO log that proves orchestrator context was injected.

    Counts the major context sections instead of dumping the prompt — keeps
    logs grep-friendly and avoids leaking large alert bodies into log search.
    """
    sections = {
        "alert_details": "Alert details (from trigger payload):" in orchestrator_thoughts,
        "triage": "Triage decision (main orchestrator reasoning):" in orchestrator_thoughts,
        "prior_synthesis": "Prior synthesis decisions" in orchestrator_thoughts,
        "original_question": "Original RCA prompt (excerpt):" in orchestrator_thoughts,
    }
    logger.info(
        "synthesis_node: orchestrator_context_injected incident=%s wave=%d "
        "orch_chars=%d prompt_chars=%d findings=%d sections=%s",
        inc_hash, target_wave, len(orchestrator_thoughts), len(full_prompt),
        findings_count, sections,
    )


def _fetch_finding_rows(incident_id: str, user_id: str, max_wave: int) -> list:
    """Return all rca_findings rows for waves 1..max_wave, ordered by wave then start time."""
    from utils.db.connection_pool import db_pool
    from utils.auth.stateless_auth import set_rls_context

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                if set_rls_context(cur, conn, user_id, log_prefix="[Synthesis]") is None:
                    logger.warning(
                        "synthesis_node: failed to set RLS context for incident %s",
                        hash_for_log(incident_id),
                    )
                    return []
                cur.execute(
                    """SELECT agent_id, role_name, storage_uri, status,
                              self_assessed_strength, wave, error_message, purpose
                       FROM rca_findings
                       WHERE incident_id = %s AND wave <= %s
                       ORDER BY wave ASC, started_at ASC""",
                    (incident_id, max_wave),
                )
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception:
        logger.exception(
            "synthesis_node: failed to fetch finding rows for %s", hash_for_log(incident_id)
        )
        return []


def _download_finding(storage_uri: str, user_id: str) -> Optional[str]:
    try:
        from utils.storage.storage import get_storage_manager
        data = get_storage_manager(user_id).download_bytes(storage_uri, user_id)
        return data.decode("utf-8") if isinstance(data, bytes) else str(data)
    except Exception:
        logger.exception("synthesis_node: failed to download finding")
        return None


def route_after_synthesis(state) -> str:
    wave = getattr(state, "synthesis_wave", 0) or 0
    inputs = getattr(state, "subagent_inputs", []) or []
    if isinstance(state, dict):
        wave = state.get("synthesis_wave", 0) or 0
        inputs = state.get("subagent_inputs", []) or []
    if wave < _MAX_SYNTHESIS_WAVES and inputs:
        return "dispatch"
    return "end"
