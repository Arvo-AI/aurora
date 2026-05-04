"""Triage node: decides single-agent vs fan-out before the first RCA wave."""

import logging
from typing import Literal

from pydantic import BaseModel

from chat.backend.agent.utils.state import State
from chat.backend.agent.orchestrator.inputs import SubAgentInput
from utils.log_sanitizer import hash_for_log

logger = logging.getLogger(__name__)

_ANTI_FANOUT_SEVERITIES = frozenset({"low", "info"})
_MAX_SUBAGENTS = 6


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

        llm = create_chat_model(model=ModelConfig.MAIN_MODEL)
        structured = llm.with_structured_output(TriageDecision)

        incident_summary = _build_triage_prompt(state, available_roles)
        decision: TriageDecision = await structured.ainvoke(incident_summary)

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


def _build_triage_prompt(state: State, available_roles: list) -> str:
    rca_context = getattr(state, "rca_context", None) or {}
    question = (getattr(state, "question", "") or "")[:1000]
    incident_id = getattr(state, "incident_id", "unknown") or "unknown"

    role_lines = "\n".join(
        f"- {r.name}: {r.description}" for r in available_roles
    ) or "(none — fan-out not viable)"

    return (
        f"You are an RCA orchestrator deciding whether to fan out to parallel sub-agents.\n\n"
        f"Incident ID: {incident_id}\n"
        f"Alert/question: {question}\n"
        f"Severity: {rca_context.get('severity', 'unknown')}\n\n"
        f"Available investigator roles (use ONLY these role_name values):\n{role_lines}\n\n"
        f"If this incident is complex enough to benefit from parallel investigation "
        f"(multiple simultaneous failure modes, cross-system correlation needed), "
        f"return mode='fanout' with up to {_MAX_SUBAGENTS} SubAgentInput objects.\n"
        f"Each input needs: agent_id (e.g. 'sa_1'), role_name (from the list above), "
        f"purpose (one bounded sentence), optional time_window.\n"
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
