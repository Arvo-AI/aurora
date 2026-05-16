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
        structured = llm.with_structured_output(SynthesisDecision, include_raw=True)

        # Pass available role names to constrain follow-up dispatch — prevents
        # the LLM from hallucinating role names that aren't in the registry.
        from chat.backend.agent.orchestrator.role_registry import RoleRegistry
        try:
            available_roles = RoleRegistry.get_instance().list_available_roles(user_id)
        except Exception:
            available_roles = []

        prompt = _build_synthesis_prompt(state, combined, current_wave, available_roles)
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

    if is_terminal:
        logger.info(
            "synthesis: incident=%s wave=%d cache_hits=%d",
            inc_hash, new_wave, get_cache_hit_count(incident_id),
        )
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
            name=DISPATCH_SUBAGENT_TOOL_NAME,
            additional_kwargs={
                "self_assessed_strength": ref.get("self_assessed_strength"),
            },
        ))
    return out


def _build_synthesis_prompt(state: State, combined_findings: str, wave: int,
                              available_roles: list) -> str:
    """Build the synthesis prompt.

    Findings are grouped by wave in `combined_findings`. The LLM judges
    `needs_more_research` from what the NEW wave (target_wave = wave+1) added,
    but writes the user-facing `summary` using full context across all waves.
    """
    incident_id = getattr(state, "incident_id", "unknown") or "unknown"
    question = (getattr(state, "question", "") or "")[:500]
    target_wave = wave + 1
    role_lines = "\n".join(
        f"- {r.name}: {r.description}" for r in available_roles
    ) or "(none available)"
    return (
        f"You are an RCA orchestrator synthesizing parallel investigation findings.\n\n"
        f"Incident: {incident_id}\nOriginal question: {question}\n"
        f"Most recent wave: {target_wave} (max {_MAX_SYNTHESIS_WAVES})\n\n"
        f"Available investigator roles for follow-up (use ONLY these role_name values):\n{role_lines}\n\n"
        f"=== SUB-AGENT FINDINGS (grouped by wave) ===\n\n{combined_findings[:12000]}\n\n"
        f"=== TASK ===\n"
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
                              self_assessed_strength, wave, error_message
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
