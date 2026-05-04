"""Synthesis node: reads all sub-agent findings and produces a unified RCA summary."""

import asyncio
import logging
from typing import Optional

from langchain_core.messages import AIMessage, ToolMessage
from pydantic import BaseModel

from chat.backend.agent.utils.state import State
from chat.backend.agent.orchestrator.inputs import SubAgentInput
from chat.backend.agent.orchestrator.dispatcher import dispatch_tool_call_id
from utils.log_sanitizer import hash_for_log

logger = logging.getLogger(__name__)

_MAX_SYNTHESIS_WAVES = 2


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
    finding_rows = _fetch_finding_rows(incident_id, user_id, target_wave)
    pending = [row for row in finding_rows if row.get("storage_uri")]
    bodies = await asyncio.gather(
        *(asyncio.to_thread(_download_finding, row["storage_uri"], user_id) for row in pending),
        return_exceptions=False,
    ) if pending else []
    finding_bodies: list[str] = [
        f"## Wave {row.get('wave', '?')} | Agent: {row.get('agent_id')} ({row.get('role_name')})\n\n{body}"
        for row, body in zip(pending, bodies) if body
    ]

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
        from chat.backend.agent.llm import ModelConfig
        from chat.backend.agent.providers import create_chat_model

        # Non-streaming: structured-output chunks must not leak into chat.
        # The user-facing summary is appended below as an AIMessage that the
        # existing chat pipeline handles.
        llm = create_chat_model(model=ModelConfig.MAIN_MODEL, streaming=False)
        structured = llm.with_structured_output(SynthesisDecision)

        prompt = _build_synthesis_prompt(state, combined, current_wave)
        decision: SynthesisDecision = await structured.ainvoke(prompt)

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

    if is_terminal:
        return {
            "synthesis_wave": new_wave,
            "subagent_inputs": [],
            "messages": new_messages,
        }

    return {
        "synthesis_wave": new_wave,
        "subagent_inputs": [inp.model_dump() for inp in decision.follow_up_inputs],
        "messages": new_messages,
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
            name="dispatch_subagent",
            additional_kwargs={
                "self_assessed_strength": ref.get("self_assessed_strength"),
            },
        ))
    return out


def _build_synthesis_prompt(state: State, combined_findings: str, wave: int) -> str:
    """Build the synthesis prompt.

    Findings are grouped by wave in `combined_findings` (each block prefixed with
    `## Wave N | ...`). The LLM must judge `needs_more_research` from what the
    NEW wave (target_wave = wave+1) added, but write the user-facing `summary`
    using full context across all waves.
    """
    incident_id = getattr(state, "incident_id", "unknown") or "unknown"
    question = (getattr(state, "question", "") or "")[:500]
    target_wave = wave + 1
    return (
        f"You are an RCA orchestrator synthesizing parallel investigation findings.\n\n"
        f"Incident: {incident_id}\nOriginal question: {question}\n"
        f"Most recent wave: {target_wave} (max {_MAX_SYNTHESIS_WAVES})\n\n"
        f"=== SUB-AGENT FINDINGS (grouped by wave) ===\n\n{combined_findings[:12000]}\n\n"
        f"=== TASK ===\n"
        f"1) needs_more_research: judge based on what wave {target_wave} added. "
        f"If the new wave (or, on wave 1, the only wave) provides a clear root cause "
        f"with at least one strong/moderate-confidence finding, return false. "
        f"If critical gaps remain that another wave could fill AND target_wave < {_MAX_SYNTHESIS_WAVES}, "
        f"return true with follow_up_inputs. Each follow_up_input needs agent_id "
        f"(e.g. sa_w{target_wave + 1}_1), role_name, purpose.\n"
        f"2) summary: a concise (3-6 sentence) user-facing markdown summary using ALL findings "
        f"across every wave shown above. Cover what was found, the most likely root cause(s), "
        f"and (if needs_more_research=true) what's being investigated next. Shown directly to the user.\n"
        f"3) rationale: brief reasoning for your decision."
    )


def _fetch_finding_rows(incident_id: str, user_id: str, max_wave: int) -> list:
    """Return all rca_findings rows for waves 1..max_wave, ordered by wave then start time."""
    from utils.db.connection_pool import db_pool
    from utils.auth.stateless_auth import set_rls_context

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[Synthesis]")
                cur.execute(
                    """SELECT agent_id, role_name, storage_uri, status,
                              self_assessed_strength, wave
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
