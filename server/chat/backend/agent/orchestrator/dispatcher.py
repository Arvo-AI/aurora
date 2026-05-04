"""Dispatcher node + router: pre-emits finding rows and emits Send objects.

Split into two functions to avoid double-execution. LangGraph runs the node
body first (returns a state update), then the conditional-edges router emits
the Sends. Using the same function for both would pre-emit DB rows twice.
"""

import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional

from langchain_core.messages import AIMessage
from langgraph.types import Send

from chat.backend.agent.utils.state import State
from chat.backend.agent.orchestrator.inputs import SubAgentInput
from utils.auth.stateless_auth import set_rls_context
from utils.db.connection_pool import db_pool
from utils.log_sanitizer import hash_for_log

logger = logging.getLogger(__name__)

_MAX_SUBAGENTS_PER_WAVE = 6


def dispatch_tool_call_id(incident_id: str, agent_id: str, wave: int) -> str:
    """Deterministic synthetic tool_call id shared by dispatch and synthesis.

    incident_id may be a UUID string. We hash it (short prefix) to keep the id
    compact and avoid leaking raw IDs in chat history.
    """
    inc_part = hashlib.sha256((incident_id or "").encode("utf-8")).hexdigest()[:12]
    return f"dispatch_{inc_part}_{agent_id}_w{wave}"


def dispatch_node(state: State) -> dict:
    """Node body: pre-emits rca_findings rows + emits a synthetic dispatch
    AIMessage so the chat UI can render the dispatch group widget. Returns a
    state update.

    The actual Send fan-out happens in dispatch_to_sub_agents (the conditional-edges
    router). Splitting them prevents the function from running twice per wave.
    """
    try:
        _pre_emit_rows(state)
    except Exception:
        logger.exception(
            "dispatch_node: pre-emit failed for incident %s",
            hash_for_log(getattr(state, "incident_id", "") or ""),
        )

    update: dict = {}
    try:
        synthetic_msg = _build_dispatch_aimessage(state)
        if synthetic_msg is not None:
            existing_messages = list(getattr(state, "messages", []) or [])
            update["messages"] = existing_messages + [synthetic_msg]
    except Exception:
        logger.exception(
            "dispatch_node: synthetic AIMessage build failed for incident %s",
            hash_for_log(getattr(state, "incident_id", "") or ""),
        )
    return update


def _filter_known_roles(raw_inputs: list) -> list:
    """Drop inputs whose role_name isn't in the registry (LLM may hallucinate)."""
    try:
        from chat.backend.agent.orchestrator.role_registry import RoleRegistry
        valid = {r.name for r in RoleRegistry.get_instance().list_all()}
    except Exception:
        return raw_inputs  # fail open — registry error shouldn't kill dispatch
    out = []
    for raw in raw_inputs:
        rn = raw.get("role_name") if isinstance(raw, dict) else getattr(raw, "role_name", None)
        if rn in valid:
            out.append(raw)
        else:
            logger.warning("dispatcher: dropping input with unknown role_name %r", rn)
    return out


def _build_dispatch_aimessage(state: State) -> Optional[AIMessage]:
    raw_inputs = getattr(state, "subagent_inputs", []) or []
    raw_inputs = _filter_known_roles(raw_inputs)
    if not raw_inputs:
        return None
    if len(raw_inputs) > _MAX_SUBAGENTS_PER_WAVE:
        raw_inputs = raw_inputs[:_MAX_SUBAGENTS_PER_WAVE]

    incident_id = getattr(state, "incident_id", "") or ""
    parent_session_id = getattr(state, "session_id", "") or ""
    wave = (getattr(state, "synthesis_wave", 0) or 0) + 1

    tool_calls: list[dict] = []
    for raw in raw_inputs:
        try:
            inp = SubAgentInput(**raw) if isinstance(raw, dict) else raw
        except Exception:
            continue
        tool_calls.append({
            "id": dispatch_tool_call_id(incident_id, inp.agent_id, wave),
            "name": "dispatch_subagent",
            "args": {
                "agent_id": inp.agent_id,
                "role_name": inp.role_name,
                "purpose": inp.purpose,
                "child_session_id": f"{parent_session_id}::sa_{inp.agent_id}",
                "wave": wave,
                "time_window": inp.time_window,
            },
        })

    if not tool_calls:
        return None

    return AIMessage(content="", tool_calls=tool_calls)


def _pre_emit_rows(state: State) -> None:
    raw_inputs = getattr(state, "subagent_inputs", []) or []
    raw_inputs = _filter_known_roles(raw_inputs)
    if not raw_inputs:
        return
    if len(raw_inputs) > _MAX_SUBAGENTS_PER_WAVE:
        raw_inputs = raw_inputs[:_MAX_SUBAGENTS_PER_WAVE]

    incident_id = getattr(state, "incident_id", None)
    user_id = getattr(state, "user_id", None)
    org_id = getattr(state, "org_id", None)
    wave = (getattr(state, "synthesis_wave", 0) or 0) + 1

    if not incident_id or not user_id:
        return

    valid_inputs: list[SubAgentInput] = []
    for raw in raw_inputs:
        try:
            valid_inputs.append(SubAgentInput(**raw) if isinstance(raw, dict) else raw)
        except Exception:
            logger.exception("dispatcher: invalid SubAgentInput %r — skipping", raw)
    if valid_inputs:
        _pre_emit_finding_rows(incident_id, valid_inputs, user_id, org_id, wave)


def dispatch_to_sub_agents(state: State) -> list:
    """Conditional-edges router: emits Send objects for each sub-agent input.

    Pure function — does NOT touch the DB. Pre-emit happens in dispatch_node.
    """
    try:
        return _build_sends(state)
    except Exception:
        logger.exception(
            "dispatcher: router error for incident %s",
            hash_for_log(getattr(state, "incident_id", "") or ""),
        )
        return []


def _build_sends(state: State) -> list:
    raw_inputs = getattr(state, "subagent_inputs", []) or []
    raw_inputs = _filter_known_roles(raw_inputs)
    if not raw_inputs:
        logger.info("dispatcher: no sub-agent inputs — empty Send list")
        return []

    if len(raw_inputs) > _MAX_SUBAGENTS_PER_WAVE:
        logger.warning(
            "dispatcher: %d inputs exceeds cap %d — truncating",
            len(raw_inputs), _MAX_SUBAGENTS_PER_WAVE,
        )
        raw_inputs = raw_inputs[:_MAX_SUBAGENTS_PER_WAVE]

    incident_id = getattr(state, "incident_id", None)
    user_id = getattr(state, "user_id", None)
    org_id = getattr(state, "org_id", None)
    parent_session_id = getattr(state, "session_id", None)
    wave = (getattr(state, "synthesis_wave", 0) or 0) + 1

    sends = []
    for raw in raw_inputs:
        try:
            inp = SubAgentInput(**raw) if isinstance(raw, dict) else raw
        except Exception:
            logger.exception("dispatcher: invalid SubAgentInput %r — skipping", raw)
            continue

        payload = {
            **inp.model_dump(),
            "parent_incident_id": incident_id,
            "parent_user_id": user_id,
            "parent_org_id": org_id,
            "parent_session_id": parent_session_id,
            "wave": wave,
        }
        sends.append(Send("sub_agent", payload))

    logger.info(
        "dispatcher: incident=%s wave=%d emitting %d sub-agent Sends",
        hash_for_log(incident_id or ""), wave, len(sends),
    )
    return sends


def _pre_emit_finding_rows(incident_id: str, inputs: list, user_id: str,
                            org_id: Optional[str], wave: int) -> None:
    """Insert/upsert rca_findings rows for all sub-agents in a single round-trip."""
    if not inputs:
        return
    try:
        now = datetime.now(timezone.utc)
        rows = [
            (incident_id, inp.agent_id, inp.role_name, inp.purpose,
             wave, now, org_id, user_id)
            for inp in inputs
        ]
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[Dispatcher]")
                cur.executemany(
                    """
                    INSERT INTO rca_findings
                        (incident_id, agent_id, role_name, purpose, status, wave,
                         started_at, org_id, user_id)
                    VALUES (%s, %s, %s, %s, 'running', %s, %s, %s, %s)
                    ON CONFLICT (incident_id, agent_id) DO UPDATE
                        SET status = 'running', started_at = EXCLUDED.started_at, wave = EXCLUDED.wave
                    """,
                    rows,
                )
            conn.commit()
    except Exception:
        logger.exception(
            "dispatcher: failed to pre-emit rca_findings rows for incident %s",
            hash_for_log(incident_id or ""),
        )
