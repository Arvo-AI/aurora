"""Triage node: decides single-agent vs fan-out before the first RCA wave."""

import asyncio
import logging
import time
from typing import Literal

from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel

from chat.backend.agent.orchestrator.usage import track_orchestrator_call
from chat.backend.agent.utils.state import State
from chat.backend.agent.orchestrator.inputs import SubAgentInput
from utils.auth.stateless_auth import set_rls_context
from utils.db.connection_pool import db_pool
from utils.log_sanitizer import hash_for_log

logger = logging.getLogger(__name__)

_ANTI_FANOUT_SEVERITIES = frozenset({"low", "info"})
_MAX_SUBAGENTS = 6
_GENERAL_INVESTIGATOR_ROLE = "general_investigator"
_PER_ROLE_CAPS = {_GENERAL_INVESTIGATOR_ROLE: 3}


def _apply_per_role_caps(inputs: list) -> list:
    """Drop excess inputs once a role's per-wave cap is hit. Preserves order.

    Defends against the LLM filling a wave with `general_investigator` spawns
    and starving the specialist roles. Order-preserving so the LLM's prioritised
    spawns survive and overflow at the tail is dropped.
    """
    if not inputs:
        return inputs
    counts: dict[str, int] = {}
    out: list = []
    for inp in inputs:
        rn = inp.get("role_name") if isinstance(inp, dict) else getattr(inp, "role_name", None)
        cap = _PER_ROLE_CAPS.get(rn or "")
        if cap is None:
            out.append(inp)
            continue
        counts[rn] = counts.get(rn, 0) + 1
        if counts[rn] > cap:
            logger.warning(
                "triage: role %r exceeds per-wave cap %d — dropping input #%d",
                rn, cap, counts[rn],
            )
            continue
        out.append(inp)
    return out


class TriageDecision(BaseModel):
    mode: Literal["single", "fanout"]
    inputs: list[SubAgentInput] = []
    rationale: str = ""


async def triage_incident(state: State) -> TriageDecision:
    try:
        return await _triage(state)
    except Exception:
        logger.exception(
            "triage_incident: unhandled error for incident %s — falling back to single",
            hash_for_log(getattr(state, "incident_id", "") or ""),
        )
        return TriageDecision(mode="single", rationale="triage error fallback")


async def _triage(state: State) -> TriageDecision:
    incident_id_hash = hash_for_log(getattr(state, "incident_id", "") or "")

    rca_context = getattr(state, "rca_context", None) or {}
    severity = str(rca_context.get("severity", "")).lower()

    if severity in _ANTI_FANOUT_SEVERITIES:
        logger.info(
            "triage: incident %s severity=%r -> single (anti-fanout heuristic)",
            incident_id_hash, severity,
        )
        return TriageDecision(mode="single", rationale=f"severity={severity} below fanout threshold")

    # Use the breadth of available investigator roles as the fan-out signal.
    # state.provider_preference only counts cloud providers (gcp/aws/azure) —
    # wrong signal: a user with GitHub + Datadog + Grafana but no cloud provider
    # would fall through to single even though 3 roles are usable.
    available_roles: list = []
    try:
        from chat.backend.agent.orchestrator.role_registry import RoleRegistry
        user_id = getattr(state, "user_id", None) or ""
        if user_id:
            available_roles = RoleRegistry.get_instance().list_available_roles(user_id)
    except Exception:
        logger.exception("triage: failed to enumerate available roles — falling back to single")

    if len(available_roles) <= 1:
        logger.info(
            "triage: incident %s available_roles=%d -> single (<=1 role usable)",
            incident_id_hash, len(available_roles),
        )
        return TriageDecision(
            mode="single",
            rationale=f"only {len(available_roles)} role(s) usable for connected integrations",
        )

    logger.info(
        "triage: incident %s available_roles=%d -> running LLM triage",
        incident_id_hash, len(available_roles),
    )

    try:
        from chat.backend.agent.llm import ModelConfig
        from chat.backend.agent.providers import create_chat_model

        if not ModelConfig.RCA_ORCHESTRATOR_MODEL:
            raise RuntimeError(
                "RCA_ORCHESTRATOR_MODEL must be set when ORCHESTRATOR_ENABLED=true"
            )
        # Non-streaming: triage's structured output is internal — must not
        # leak token chunks into the user-facing chat stream.
        llm = create_chat_model(model=ModelConfig.RCA_ORCHESTRATOR_MODEL, streaming=False)
        structured = llm.with_structured_output(
            TriageDecision, include_raw=True, method="function_calling"
        )

        incident_summary = await _build_triage_prompt(state, available_roles)
        start_time = time.time()
        result = await structured.ainvoke(incident_summary)

        decision = result.get("parsed") or TriageDecision(
            mode="single", rationale="triage parse error fallback"
        )
        await asyncio.to_thread(
            track_orchestrator_call,
            state, result.get("raw"), incident_summary,
            "triage_decision", start_time,
        )

        # Defense-in-depth: clamp to the role allowlist. Mirrors synthesis_node.
        valid_role_names = {role.name for role in available_roles}
        if decision.mode != "fanout":
            decision.inputs = []
        else:
            decision.inputs = [
                inp for inp in decision.inputs if inp.role_name in valid_role_names
            ]
            if not decision.inputs:
                return TriageDecision(
                    mode="single",
                    rationale="LLM returned no valid sub-agent roles",
                )

            decision.inputs = _apply_per_role_caps(decision.inputs)
            if not decision.inputs:
                return TriageDecision(
                    mode="single",
                    rationale="LLM returned no valid sub-agent roles after per-role caps",
                )

        if len(decision.inputs) > _MAX_SUBAGENTS:
            logger.warning(
                "triage: LLM produced %d inputs > cap %d — truncating",
                len(decision.inputs), _MAX_SUBAGENTS,
            )
            decision.inputs = decision.inputs[:_MAX_SUBAGENTS]

        logger.info(
            "triage: incident %s -> mode=%s sub-agents=%d",
            incident_id_hash, decision.mode, len(decision.inputs),
        )
        return decision

    except Exception:
        logger.exception(
            "triage: LLM call failed for incident %s — falling back to single",
            incident_id_hash,
        )
        return TriageDecision(mode="single", rationale="LLM triage error fallback")


def _fetch_prior_findings_count(incident_id: str, user_id: str) -> int:
    """Return count of rca_findings rows for this incident. RLS-safe, never raises."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                if set_rls_context(cur, conn, user_id, log_prefix="[Triage]") is None:
                    logger.warning(
                        "triage: failed to set RLS context for findings count, incident %s",
                        hash_for_log(incident_id),
                    )
                    return 0
                cur.execute(
                    "SELECT COUNT(*) FROM rca_findings WHERE incident_id = %s",
                    (incident_id,),
                )
                row = cur.fetchone()
                return int(row[0]) if row else 0
    except Exception:
        logger.warning(
            "triage: could not fetch findings count for %s — proceeding with 0",
            hash_for_log(incident_id),
        )
        return 0


def _build_conversation_context(state: State) -> tuple[int, str]:
    """Return (ai_turn_count, recent_transcript) from state.messages. ToolMessages excluded."""
    messages = getattr(state, "messages", None) or []
    ai_turns = sum(1 for m in messages if isinstance(m, AIMessage))
    convo = [m for m in messages if isinstance(m, (HumanMessage, AIMessage))]
    recent = convo[-4:] if len(convo) >= 4 else convo
    lines: list[str] = []
    for m in recent:
        role = "user" if isinstance(m, HumanMessage) else "assistant"
        content = getattr(m, "content", "") or ""
        if isinstance(content, list):
            content = " ".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content
            )
        lines.append(f"{role}: {str(content)[:300]}")
    return ai_turns, "\n".join(lines)


async def _build_triage_prompt(state: State, available_roles: list) -> str:
    rca_context = getattr(state, "rca_context", None) or {}
    question = (getattr(state, "question", "") or "")[:1000]
    incident_id = getattr(state, "incident_id", "unknown") or "unknown"
    user_id = getattr(state, "user_id", None) or ""

    role_lines = "\n".join(
        f"- {r.name}: {r.description}" for r in available_roles
    ) or "(none — fan-out not viable)"

    # Situational context lets the LLM distinguish a fresh incident trigger from
    # a follow-up message on an already-investigated one (e.g. "thank you").
    # Each follow-up may live in its own session, so chat history alone is not
    # a reliable signal — query findings for the incident regardless.
    ai_turn_count, recent_transcript = _build_conversation_context(state)
    prior_findings_count = 0
    if incident_id != "unknown" and user_id:
        prior_findings_count = await asyncio.to_thread(
            _fetch_prior_findings_count, incident_id, user_id
        )

    context_block = (
        f"=== SITUATIONAL CONTEXT ===\n"
        f"Conversation turn: {ai_turn_count} (0 = initial alert, no prior assistant response)\n"
        f"Prior RCA findings on file for this incident: {prior_findings_count}\n"
        f"Recent transcript (last 4 user/assistant turns, tool calls excluded):\n"
        f"{recent_transcript if recent_transcript else '(none yet)'}\n"
        f"=== END CONTEXT ===\n\n"
    )

    gen_cap = _PER_ROLE_CAPS[_GENERAL_INVESTIGATOR_ROLE]
    return (
        f"You are an RCA orchestrator deciding whether to fan out to parallel sub-agents.\n\n"
        f"Incident ID: {incident_id}\n"
        f"Alert/question: {question}\n"
        f"Severity: {rca_context.get('severity', 'unknown')}\n\n"
        f"{context_block}"
        f"Available investigator roles (use ONLY these role_name values):\n{role_lines}\n\n"
        f"Decision rule: choose mode='fanout' ONLY if the latest message genuinely requires "
        f"NEW parallel investigation that prior findings don't already cover — typically "
        f"because the incident has multiple simultaneous failure modes or needs cross-system "
        f"correlation. If prior findings already address the user's question, or the message "
        f"is conversational (acknowledgements, clarifications about prior findings, simple "
        f"read-questions), choose mode='single'.\n"
        f"For mode='fanout', emit up to {_MAX_SUBAGENTS} SubAgentInput objects.\n"
        f"Each input needs: agent_id (e.g. 'sa_1'), role_name (from the list above), "
        f"purpose (one bounded sentence), optional time_window, optional extra_constraints "
        f"(dict of focus/boundary keys for the sub-agent).\n\n"
        f"Role selection guidance:\n"
        f"- Prefer a specialist role when one clearly fits the sub-question.\n"
        f"- For sub-questions that don't cleanly map to a specialist (e.g. DNS records, "
        f"TLS/cert validity, third-party API health, queue depth, data correctness, "
        f"config drift, IAM/permission audits, build provenance, anything novel), use "
        f"`general_investigator` rather than skipping the sub-question or forcing it "
        f"into a specialist.\n"
        f"- You MAY emit multiple `general_investigator` inputs in the same wave (up to "
        f"{gen_cap}) when the incident has multiple distinct novel sub-questions. Give each "
        f"a UNIQUE agent_id and a distinct, tightly-bounded `purpose`. Use `extra_constraints` "
        f"like {{\"focus\": \"DNS records for foo.example.com\", \"boundary\": \"do not "
        f"investigate TLS\"}} to keep parallel general investigators in non-overlapping lanes.\n"
        f"- agent_id values MUST be unique across all inputs in this wave.\n\n"
        f"If the incident is straightforward, return mode='single' with empty inputs.\n"
        f"Always include a brief rationale."
    )


async def triage_node(state: State) -> dict:
    try:
        decision = await triage_incident(state)
        return {
            "triage_decision": decision.model_dump(),
            "subagent_inputs": [inp.model_dump() for inp in decision.inputs],
        }
    except Exception:
        logger.exception("triage_node: unexpected error — routing to single-agent")
        return {
            "triage_decision": {"mode": "single", "inputs": [], "rationale": "node error fallback"},
            "subagent_inputs": [],
        }


def route_triage(state) -> str:
    td = getattr(state, "triage_decision", None)
    if td is None and isinstance(state, dict):
        td = state.get("triage_decision")
    if isinstance(td, dict):
        mode = td.get("mode", "single")
    else:
        mode = getattr(td, "mode", "single") if td else "single"

    is_bg = getattr(state, "is_background", False)
    if isinstance(state, dict):
        is_bg = state.get("is_background", False)
    if not is_bg:
        return "direct_react"
    return "dispatch" if mode == "fanout" else "direct_react"
