"""Sub-agent subgraph factory — depth-1 worker, no further spawning."""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime
from typing import Any, Optional

from langgraph.graph import END, START, StateGraph

from chat.backend.agent.subagent.findings_writer import write_findings
from chat.backend.agent.subagent.prompts import build_subagent_system_prompt
from chat.backend.agent.subagent.state import SubAgentResult, SubAgentState

logger = logging.getLogger(__name__)


_DEFAULT_RECURSION_LIMIT = 50
_DEFAULT_SUBAGENT_WALLCLOCK_SECONDS = 240
_DEFAULT_SUBAGENT_TOKEN_BUDGET = 300_000
_GLOBAL_DEFAULT_ORG_ID = "00000000-0000-0000-0000-000000000000"


def _resolve_subagent_config_value(org_id: Optional[str], column: str, default: int) -> int:
    if not org_id:
        return default
    try:
        from utils.db.connection_pool import db_pool

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SET myapp.current_org_id = %s", (org_id,))
                cur.execute(
                    f"SELECT {column} FROM multi_agent_config "
                    f"WHERE org_id IN (%s, %s) "
                    f"ORDER BY (org_id = %s) DESC LIMIT 1",  # org row wins; fall back to global
                    (org_id, _GLOBAL_DEFAULT_ORG_ID, org_id),
                )
                row = cur.fetchone()
                if row and row[0]:
                    return int(row[0])
    except Exception as e:
        logger.warning("[subagent:_resolve_subagent_config_value:%s] %s", column, e)
    return default


def _resolve_subagent_wallclock_cap(org_id: Optional[str]) -> int:
    return _resolve_subagent_config_value(
        org_id, "per_subagent_wallclock_seconds", _DEFAULT_SUBAGENT_WALLCLOCK_SECONDS
    )


def _resolve_subagent_token_budget(org_id: Optional[str]) -> int:
    return _resolve_subagent_config_value(
        org_id, "per_subagent_token_budget", _DEFAULT_SUBAGENT_TOKEN_BUDGET
    )


def _check_token_budget_or_cancel(state: SubAgentState, budget: int) -> None:
    if not state.incident_id or not state.agent_id:
        return
    try:
        from chat.backend.agent.llm import get_token_spend
    except ImportError:
        logger.info("[subagent:_check_token_budget] get_token_spend unavailable; skipping")
        return
    try:
        spent = int(get_token_spend(state.incident_id, state.org_id, agent_id=state.agent_id) or 0)
    except Exception as e:
        logger.warning("[subagent:_check_token_budget] get_token_spend failed: %s", e)
        return
    if spent > 0 and spent > budget:
        raise asyncio.CancelledError(
            f"per-subagent token budget exceeded ({spent}/{budget})"
        )


def _serialize_messages_to_markdown(
    *,
    agent_id: str,
    purpose: str,
    messages: list,
) -> str:
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

    lines: list[str] = []
    lines.append(f"# Sub-agent transcript: {agent_id}")
    lines.append(f"Purpose: {purpose}")
    lines.append("")

    for msg in messages:
        if isinstance(msg, SystemMessage):
            header = "## [SystemMessage]"
        elif isinstance(msg, HumanMessage):
            header = "## [HumanMessage]"
        elif isinstance(msg, AIMessage):
            header = "## [AIMessage]"
        elif isinstance(msg, ToolMessage):
            tool_name = getattr(msg, "name", "") or "unknown_tool"
            header = f"## [ToolMessage] {tool_name}"
        else:
            header = f"## [{type(msg).__name__}]"

        content = getattr(msg, "content", "")
        if isinstance(content, list):
            content = "\n".join(
                p.get("text", "") if isinstance(p, dict) else str(p) for p in content
            )
        elif not isinstance(content, str):
            content = str(content)

        lines.append(header)
        lines.append(content)

        if isinstance(msg, AIMessage):
            tool_calls = getattr(msg, "tool_calls", None) or []
            if tool_calls:
                lines.append("")
                lines.append("### tool_calls")
                for tc in tool_calls:
                    name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
                    args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
                    lines.append(f"- {name}: {args}")
        lines.append("")

    return "\n".join(lines)


def _persist_transcript(
    *,
    org_id: Optional[str],
    incident_id: Optional[str],
    agent_id: Optional[str],
    purpose: str,
    messages: list,
) -> Optional[str]:
    if not org_id or not incident_id or not agent_id:
        return None
    try:
        from utils.storage.storage import get_storage_manager

        markdown = _serialize_messages_to_markdown(
            agent_id=agent_id,
            purpose=purpose,
            messages=messages,
        )
        storage_path = f"subagent_transcripts/{org_id}/{incident_id}/{agent_id}.md"
        storage = get_storage_manager()
        storage.upload_bytes(
            data=markdown.encode("utf-8"),
            path=storage_path,
            content_type="text/markdown",
        )
        return storage_path
    except Exception as e:
        logger.warning("[subagent:_persist_transcript] failed for %s: %s", agent_id, e)
        return None


def _safe_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


async def _record_event_safe(
    *,
    session_id: Optional[str],
    org_id: Optional[str],
    type_: str,
    payload: dict,
    agent_id: Optional[str],
    parent_agent_id: Optional[str],
) -> None:
    if not session_id or not org_id:
        return
    try:
        from chat.backend.agent.utils.persistence.chat_events import record_event
        await record_event(
            session_id=session_id,
            org_id=org_id,
            type=type_,
            payload=payload,
            agent_id=agent_id,
            parent_agent_id=parent_agent_id,
        )
    except Exception as e:
        logger.warning(f"[subagent] record_event(type={type_}) failed: {e}")


def _insert_subagent_run(state: SubAgentState, model_used: str) -> None:
    if not state.user_id or not state.incident_id or not state.session_id:
        logger.warning(
            f"[subagent] skipping incident_subagent_runs insert — missing ids "
            f"(user={bool(state.user_id)} incident={bool(state.incident_id)} session={bool(state.session_id)})"
        )
        return
    try:
        from utils.auth.stateless_auth import set_rls_context
        from utils.db.connection_pool import db_pool

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                org_id = set_rls_context(cur, conn, state.user_id, log_prefix="[SubAgent:insert]")
                if not org_id:
                    return
                cur.execute(
                    """
                    INSERT INTO incident_subagent_runs
                        (incident_id, session_id, agent_id, parent_agent_id, role,
                         delegate_level, purpose, suggested_skill_focus, model_used,
                         status, org_id, started_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    """,
                    (
                        state.incident_id,
                        state.session_id,
                        state.agent_id,
                        state.parent_agent_id,
                        "subagent",
                        state.delegate_level,
                        state.purpose,
                        list(state.suggested_skill_focus or []),
                        model_used,
                        "running",
                        state.org_id,
                    ),
                )
                conn.commit()
    except Exception as e:
        logger.warning(f"[subagent] incident_subagent_runs insert failed for {state.agent_id}: {e}")


def _update_subagent_run(
    state: SubAgentState,
    *,
    status: str,
    findings_artifact_ref: Optional[str],
    error: Optional[str],
    self_assessed_strength: Optional[str],
    transcript_ref: Optional[str] = None,
) -> None:
    if not state.user_id or not state.session_id:
        return
    try:
        from utils.auth.stateless_auth import set_rls_context
        from utils.db.connection_pool import db_pool

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                org_id = set_rls_context(cur, conn, state.user_id, log_prefix="[SubAgent:update]")
                if not org_id:
                    return
                cur.execute(
                    """
                    UPDATE incident_subagent_runs
                    SET status = %s,
                        ended_at = NOW(),
                        findings_artifact_ref = %s,
                        error = %s,
                        self_assessed_strength = %s,
                        transcript_ref = COALESCE(%s, transcript_ref)
                    WHERE agent_id = %s AND session_id = %s
                    """,
                    (
                        status,
                        findings_artifact_ref,
                        error,
                        self_assessed_strength,
                        transcript_ref,
                        state.agent_id,
                        state.session_id,
                    ),
                )
                conn.commit()
    except Exception as e:
        logger.warning(f"[subagent] incident_subagent_runs update failed for {state.agent_id}: {e}")


def _build_tools_for_subagent(state: SubAgentState):
    from utils.cloud.cloud_utils import set_user_context
    from chat.backend.agent.tools.cloud_tools import get_cloud_tools

    set_user_context(
        user_id=state.user_id,
        session_id=state.session_id,
        provider_preference=None,
        selected_project_id=None,
        state=None,
        mode="subagent",
    )

    return get_cloud_tools(agent_id=state.agent_id)


async def _run_react_loop(
    *,
    state: SubAgentState,
    system_prompt: str,
    tools: list,
    provider: str,
    model_id: str,
) -> dict[str, Any]:
    from langchain.agents import create_agent
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from chat.backend.agent.providers import create_chat_model

    model_name = f"{provider}/{model_id}" if "/" not in model_id else model_id
    llm = create_chat_model(model_name, temperature=0.2)

    agent_graph = create_agent(
        model=llm,
        tools=tools,
        system_prompt=system_prompt,
    )

    recursion_limit = _safe_int_env("SUBAGENT_RECURSION_LIMIT", _DEFAULT_RECURSION_LIMIT)

    user_message = HumanMessage(
        content=(state.incident_summary or "(no incident summary provided)").strip()
    )

    wallclock_cap = _resolve_subagent_wallclock_cap(state.org_id)
    token_budget = _resolve_subagent_token_budget(state.org_id)

    _check_token_budget_or_cancel(state, token_budget)

    messages: list = []
    transcript_ref: Optional[str] = None
    try:
        result = await asyncio.wait_for(
            agent_graph.ainvoke(
                {"messages": [user_message]},
                config={"recursion_limit": recursion_limit},
            ),
            timeout=wallclock_cap,
        )
        messages = result.get("messages", []) if isinstance(result, dict) else []
    finally:
        try:
            transcript_ref = _persist_transcript(
                org_id=state.org_id,
                incident_id=state.incident_id,
                agent_id=state.agent_id,
                purpose=state.purpose,
                messages=messages,
            )
        except Exception as e:
            logger.warning("[subagent] transcript persistence skipped: %s", e)
            transcript_ref = None

    tools_used: list[str] = []
    evidence: list[dict] = []
    final_text = ""

    for msg in messages:
        if isinstance(msg, AIMessage):
            tool_calls = getattr(msg, "tool_calls", None) or []
            for tc in tool_calls:
                name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
                if name and name not in tools_used:
                    tools_used.append(name)
            content = msg.content
            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "") if isinstance(p, dict) else str(p) for p in content
                )
            if isinstance(content, str) and content.strip():
                final_text = content.strip()
        elif isinstance(msg, ToolMessage):
            tool_name = getattr(msg, "name", "unknown_tool")
            obs = msg.content if isinstance(msg.content, str) else str(msg.content)
            if len(obs) > 800:
                obs = obs[:800] + "...[truncated]"
            evidence.append({
                "text": f"{tool_name}: {obs}",
                "citation": getattr(msg, "tool_call_id", "") or tool_name,
            })

    summary, reasoning, ruled_out = _split_final_response(final_text)

    return {
        "tools_used": tools_used,
        "summary": summary or final_text or "(no summary produced)",
        "evidence": evidence,
        "reasoning": reasoning or "(no reasoning section produced)",
        "ruled_out": ruled_out,
        "citations": [e.get("citation", "") for e in evidence if e.get("citation")],
        "transcript_ref": transcript_ref,
    }


def _split_final_response(text: str) -> tuple[str, str, list[str]]:
    if not text:
        return "", "", []
    sections: dict[str, list[str]] = {}
    current: Optional[str] = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            current = stripped[3:].strip().lower()
            sections[current] = []
        elif current is not None:
            sections[current].append(line)

    def _join(key: str) -> str:
        return "\n".join(sections.get(key, [])).strip()

    summary = _join("summary")
    reasoning = _join("reasoning")
    ruled_text = _join("what i ruled out")
    ruled_out = [
        ln.lstrip("- ").strip()
        for ln in ruled_text.splitlines()
        if ln.strip() and ln.strip() != "(nothing ruled out)"
    ]
    return summary, reasoning, ruled_out


async def _setup_node(state: SubAgentState) -> dict[str, Any]:
    from chat.backend.agent.llm import resolve_role_model

    updates: dict[str, Any] = {}
    if not state.agent_id:
        updates["agent_id"] = f"sub-{uuid.uuid4().hex[:8]}"

    effective_state = state if state.agent_id else state.model_copy(update=updates)

    try:
        provider, model_id = resolve_role_model("subagent", state.user_id, state.org_id)
    except Exception as e:
        logger.warning(f"[subagent] resolve_role_model failed, falling back: {e}")
        provider, model_id = "anthropic", "claude-sonnet-4.6"

    model_used = f"{provider}/{model_id}"
    updates["model_used"] = model_used

    _insert_subagent_run(effective_state, model_used)

    await _record_event_safe(
        session_id=state.session_id,
        org_id=state.org_id,
        type_="subagent_dispatched",
        payload={
            "agent_id": effective_state.agent_id,
            "purpose": state.purpose,
            "model": model_used,
            "suggested_skill_focus": list(state.suggested_skill_focus or []),
        },
        agent_id=effective_state.agent_id,
        parent_agent_id=state.parent_agent_id,
    )

    return updates


async def _react_loop_node(state: SubAgentState) -> dict[str, Any]:
    from chat.backend.agent.llm import resolve_role_model

    try:
        system_prompt = build_subagent_system_prompt(
            purpose=state.purpose,
            suggested_skill_focus=state.suggested_skill_focus,
            kb_memory=state.kb_memory,
        )
    except Exception as e:
        logger.exception(f"[subagent] system prompt build failed: {e}")
        return {"status": "failed", "error": f"prompt_build_failed: {e}"}

    try:
        provider, model_id = resolve_role_model("subagent", state.user_id, state.org_id)
    except Exception:
        provider, model_id = "anthropic", "claude-sonnet-4.6"

    try:
        tools = _build_tools_for_subagent(state)
    except Exception as e:
        logger.exception(f"[subagent] tool list build failed: {e}")
        return {"status": "failed", "error": f"tools_build_failed: {e}"}

    try:
        captured = await _run_react_loop(
            state=state,
            system_prompt=system_prompt,
            tools=tools,
            provider=provider,
            model_id=model_id,
        )
    except asyncio.CancelledError as e:
        logger.warning(f"[subagent] ReAct loop cancelled for {state.agent_id}: {e}")
        return {"status": "cancelled", "error": str(e) or "cancelled"}
    except Exception as e:
        logger.exception(f"[subagent] ReAct loop failed for {state.agent_id}: {e}")
        return {"status": "failed", "error": str(e)}

    return {
        "tools_used": captured["tools_used"],
        "status": "succeeded",
        "react_capture": captured,
    }


def _get_capture(state: SubAgentState) -> dict[str, Any]:
    cap = getattr(state, "react_capture", None)
    return cap if isinstance(cap, dict) else {}


async def _write_findings_node(state: SubAgentState) -> dict[str, Any]:
    captured: dict[str, Any] = _get_capture(state)
    status = state.status if state.status in ("succeeded", "failed", "cancelled") else "succeeded"
    self_assessed_strength = "medium"

    if status in ("failed", "cancelled"):
        try:
            human_label = "Investigation incomplete." if status == "failed" else "Investigation cancelled."
            artifact_ref = write_findings(
                state=state,
                summary=human_label + " No conclusion reached for this sub-agent.",
                evidence=[],
                reasoning="The sub-agent did not converge on a finding within its iteration / time budget.",
                ruled_out=[],
                citations=[],
                self_assessed_strength="inconclusive",
            )
            self_assessed_strength = "inconclusive"
        except Exception as e:
            logger.exception(f"[subagent] write_findings ({status}-path) error: {e}")
            artifact_ref = None
    else:
        try:
            artifact_ref = write_findings(
                state=state,
                summary=captured.get("summary", "(no summary)"),
                evidence=captured.get("evidence", []),
                reasoning=captured.get("reasoning", "(no reasoning)"),
                ruled_out=captured.get("ruled_out", []),
                citations=captured.get("citations", []),
                self_assessed_strength=self_assessed_strength,
            )
        except Exception as e:
            logger.exception(f"[subagent] write_findings failed: {e}")
            status = "failed"
            artifact_ref = None
            self_assessed_strength = "inconclusive"
            state = state.model_copy(update={"status": "failed", "error": str(e)})

    transcript_ref = captured.get("transcript_ref") if isinstance(captured, dict) else None

    _update_subagent_run(
        state,
        status=status,
        findings_artifact_ref=artifact_ref,
        error=state.error if status in ("failed", "cancelled") else None,
        self_assessed_strength=self_assessed_strength,
        transcript_ref=transcript_ref,
    )

    event_type = "subagent_finished" if status == "succeeded" else "subagent_failed"
    await _record_event_safe(
        session_id=state.session_id,
        org_id=state.org_id,
        type_=event_type,
        payload={
            "agent_id": state.agent_id,
            "artifact_ref": artifact_ref,
            "tools_used": list(state.tools_used or []),
            "status": status,
            "error": state.error if status in ("failed", "cancelled") else None,
        },
        agent_id=state.agent_id,
        parent_agent_id=state.parent_agent_id,
    )

    result = SubAgentResult(
        agent_id=state.agent_id,
        purpose=state.purpose,
        status=status,  # type: ignore[arg-type]
        findings_artifact_ref=artifact_ref,
        error=state.error if status in ("failed", "cancelled") else None,
    )
    return {
        "findings_artifact_ref": artifact_ref,
        "status": status,
        "subagent_results": [result.model_dump()],
    }


def build_subagent_subgraph(delegate_level: int = 1):
    # No checkpointer — parent provides per-invocation isolation.
    # No sub-agent dispatch tool exposed inside the loop — depth-1 cap.
    graph: StateGraph = StateGraph(SubAgentState)
    graph.add_node("setup", _setup_node)
    graph.add_node("react_loop", _react_loop_node)
    graph.add_node("write_findings", _write_findings_node)

    graph.add_edge(START, "setup")
    graph.add_edge("setup", "react_loop")
    graph.add_edge("react_loop", "write_findings")
    graph.add_edge("write_findings", END)

    return graph.compile()
