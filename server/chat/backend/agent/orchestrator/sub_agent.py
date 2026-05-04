"""Sub-agent node: runs one bounded ReAct investigation and writes findings.md."""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from chat.backend.agent.orchestrator.inputs import FindingRef, SubAgentInput
from chat.backend.agent.orchestrator.findings_schema import make_stub
from utils.log_sanitizer import hash_for_log

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 180
_MAX_HISTORY_ENTRIES = 30
_MAX_HISTORY_FIELD_CHARS = 1000
_FINDING_REF_STATUSES = frozenset({"succeeded", "failed", "timeout", "cancelled"})


def _truncate(value, limit: int = _MAX_HISTORY_FIELD_CHARS) -> str:
    if value is None:
        return ""
    s = value if isinstance(value, str) else str(value)
    return s if len(s) <= limit else s[:limit] + "...[truncated]"


def _extract_tool_call_history(tool_capture) -> list[dict]:
    """Serialize ToolContextCapture's per-session tool calls as a small list.

    Best-effort: anything unexpected returns an empty list rather than raising.
    """
    if tool_capture is None:
        return []
    try:
        raw = getattr(tool_capture, "current_tool_calls", {}) or {}
        items: list[dict] = []
        for call_id, info in raw.items():
            if not isinstance(info, dict):
                continue
            started = info.get("start_time")
            completed = info.get("completion_time")
            try:
                started_iso = started.isoformat() if started else None
            except Exception:
                started_iso = None
            try:
                completed_iso = completed.isoformat() if completed else None
            except Exception:
                completed_iso = None
            status = "completed" if info.get("completed") else "running"
            items.append({
                "tool_name": _truncate(info.get("tool_name") or "unknown", 128),
                "args": _truncate(info.get("input"), _MAX_HISTORY_FIELD_CHARS),
                "output_excerpt": _truncate(info.get("output_excerpt") or "", _MAX_HISTORY_FIELD_CHARS),
                "is_error": bool(info.get("is_error", False)),
                "status": status,
                "started_at": started_iso,
                "completed_at": completed_iso,
            })

        # Order by started_at, fall back to insertion order
        try:
            items.sort(key=lambda d: d.get("started_at") or "")
        except Exception:
            pass
        return items[:_MAX_HISTORY_ENTRIES]
    except Exception:
        logger.exception("sub_agent: tool_call_history extraction failed")
        return []


def _read_summary_from_storage(incident_id: str, agent_id: str, user_id: str) -> Optional[str]:
    """Read the ## Summary section from findings.md if it exists."""
    try:
        from utils.storage.storage import get_storage_manager
        storage_uri = f"rca/{incident_id}/findings/{agent_id}.md"
        data = get_storage_manager(user_id).download_bytes(storage_uri, user_id)
        body = data.decode("utf-8") if isinstance(data, bytes) else str(data)
        # Extract ## Summary section
        marker = "## Summary"
        idx = body.find(marker)
        if idx == -1:
            return None
        after = body[idx + len(marker):].lstrip()
        # Stop at next H2
        end = after.find("\n## ")
        if end != -1:
            after = after[:end]
        text = after.strip()
        return text[:500] if text else None
    except Exception:
        return None


async def sub_agent_node(input_dict: dict) -> dict:
    agent_id = input_dict.get("agent_id", "unknown")
    incident_id = input_dict.get("parent_incident_id", "")
    wave = input_dict.get("wave")
    inc_hash = hash_for_log(incident_id or "")

    try:
        ref = await _run_with_timeout(input_dict)
    except Exception:
        logger.exception("sub_agent_node: unhandled error agent=%s incident=%s", agent_id, inc_hash)
        ref = FindingRef(
            agent_id=agent_id,
            role_name=input_dict.get("role_name", ""),
            storage_uri=None,
            status="failed",
            error_message="unhandled node error",
        )

    if ref.wave is None and wave is not None:
        try:
            ref.wave = int(wave)
        except (TypeError, ValueError):
            pass

    return {"finding_refs": [ref.model_dump()]}


async def _run_with_timeout(input_dict: dict) -> FindingRef:
    agent_id = input_dict.get("agent_id", "unknown")
    incident_id = input_dict.get("parent_incident_id", "")
    user_id = input_dict.get("parent_user_id", "")
    role_name = input_dict.get("role_name", "")
    inc_hash = hash_for_log(incident_id or "")

    timeout = _DEFAULT_TIMEOUT_SECONDS
    try:
        from chat.backend.agent.orchestrator.role_registry import RoleRegistry
        role_meta = RoleRegistry.get_instance().get(role_name)
        if role_meta:
            timeout = role_meta.max_seconds
    except Exception:
        pass

    try:
        return await asyncio.wait_for(_run(input_dict), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning(
            "sub_agent_node: timeout agent=%s incident=%s after %ds",
            agent_id, inc_hash, timeout,
        )
        # Recover any tool calls captured before the timeout. _run set the
        # contextvar via set_tool_capture(...) and asyncio.wait_for runs the
        # inner coro in the same task, so the contextvar is still live here.
        history: list = []
        try:
            from utils.cloud.cloud_utils import _tool_capture_var
            partial_capture = _tool_capture_var.get()
            if partial_capture is not None:
                history = _extract_tool_call_history(partial_capture)
        except Exception:
            logger.exception("sub_agent: failed to recover tool_call_history on timeout")
        _write_stub_to_storage(agent_id, role_name, incident_id, user_id, "timeout", "timed out")
        _update_db_terminal(
            agent_id, incident_id, user_id, input_dict.get("parent_org_id"),
            "timeout", tool_call_history=history,
        )
        return FindingRef(
            agent_id=agent_id, role_name=role_name,
            storage_uri=f"rca/{incident_id}/findings/{agent_id}.md",
            status="timeout",
            summary=f"Sub-agent {agent_id} ({role_name}) timed out after {timeout}s",
        )


async def _run(input_dict: dict) -> FindingRef:
    try:
        inp = SubAgentInput(
            agent_id=input_dict["agent_id"],
            role_name=input_dict["role_name"],
            purpose=input_dict["purpose"],
            time_window=input_dict.get("time_window"),
            evidence_refs=input_dict.get("evidence_refs", []),
            extra_constraints=input_dict.get("extra_constraints"),
        )
    except Exception as exc:
        logger.error("sub_agent: SubAgentInput validation failed: %s", exc)
        agent_id = input_dict.get("agent_id", "unknown")
        role_name = input_dict.get("role_name", "")
        incident_id = input_dict.get("parent_incident_id", "")
        user_id = input_dict.get("parent_user_id", "")
        org_id = input_dict.get("parent_org_id")
        if incident_id and user_id:
            _write_stub_to_storage(agent_id, role_name, incident_id, user_id,
                                   "failed", f"input validation: {exc}")
            _update_db_terminal(agent_id, incident_id, user_id, org_id, "failed",
                                tool_call_history=[])
        return FindingRef(
            agent_id=agent_id, role_name=role_name,
            storage_uri=f"rca/{incident_id}/findings/{agent_id}.md" if incident_id else None,
            status="failed",
            error_message=f"input validation: {exc}",
        )

    incident_id = input_dict.get("parent_incident_id", "")
    user_id = input_dict.get("parent_user_id", "")
    org_id = input_dict.get("parent_org_id")
    parent_session_id = input_dict.get("parent_session_id", "") or ""
    child_session_id = f"{parent_session_id}::sa_{inp.agent_id}"
    inc_hash = hash_for_log(incident_id)

    logger.info(
        "sub_agent: starting agent=%s role=%s incident=%s",
        inp.agent_id, inp.role_name, inc_hash,
    )

    try:
        from utils.cloud.cloud_utils import set_user_context, set_tool_capture
        from chat.backend.agent.utils.tool_context_capture import ToolContextCapture

        set_user_context(
            user_id=user_id,
            session_id=child_session_id,
            provider_preference=None,
            selected_project_id=None,
            state=None,
            mode="ask",
        )
        tool_capture = ToolContextCapture(
            session_id=child_session_id,
            user_id=user_id,
            incident_id=incident_id,
            org_id=org_id,
        )
        set_tool_capture(tool_capture)
    except Exception:
        logger.exception("sub_agent: failed to bind ContextVars for agent %s", inp.agent_id)
        tool_capture = None

    from chat.backend.agent.orchestrator.role_registry import RoleRegistry
    role_meta = RoleRegistry.get_instance().get(inp.role_name)
    if not role_meta:
        logger.error("sub_agent: role %r not found in registry", inp.role_name)
        if incident_id and user_id:
            _write_stub_to_storage(inp.agent_id, inp.role_name, incident_id, user_id,
                                   "failed", f"role {inp.role_name!r} not found")
            _update_db_terminal(inp.agent_id, incident_id, user_id, org_id, "failed",
                                tool_call_history=[])
        return FindingRef(
            agent_id=inp.agent_id, role_name=inp.role_name,
            storage_uri=f"rca/{incident_id}/findings/{inp.agent_id}.md" if incident_id else None,
            status="failed",
            error_message=f"role {inp.role_name!r} not found",
        )

    from chat.backend.agent.orchestrator.inputs import render_brief
    from chat.backend.agent.orchestrator.select_skills import (
        load_skills_for_role,
        select_tools_for_role,
    )
    from chat.backend.agent.orchestrator.findings_writer import make_write_findings_tool
    from chat.backend.agent.tools.cloud_tools import get_cloud_tools

    brief = render_brief(inp, role_meta)
    skill_content = load_skills_for_role(user_id, role_meta)
    if skill_content:
        brief = brief + "\n\n## Integration-Specific Guidance\n\n" + skill_content
    all_tools = get_cloud_tools()
    role_tools = select_tools_for_role(user_id, role_meta, all_tools)
    write_tool = make_write_findings_tool(
        agent_id=inp.agent_id, role_name=inp.role_name,
        incident_id=incident_id, user_id=user_id, org_id=org_id,
        child_session_id=child_session_id,
    )
    tools = role_tools + [write_tool]

    try:
        from chat.backend.agent.agent import Agent
        from chat.backend.agent.db import PostgreSQLClient
        from chat.backend.agent.weaviate_client import WeaviateClient
        from chat.backend.agent.utils.state import State
        from langchain_core.messages import HumanMessage

        sub_state = State(
            question=brief,
            messages=[HumanMessage(content=brief)],
            user_id=user_id,
            session_id=child_session_id,
            incident_id=incident_id,
            org_id=org_id,
            is_background=True,
            mode="ask",
        )

        postgres_client = PostgreSQLClient()
        agent = Agent(
            weaviate_client=WeaviateClient(postgres_client),
            postgres_client=postgres_client,
        )
        if tool_capture is not None:
            agent.set_tool_capture(tool_capture)

        await agent.agentic_tool_flow(
            sub_state,
            system_prompt_override=brief,
            tool_subset=tools,
            max_turns=role_meta.max_turns,
        )

        logger.info("sub_agent: agent completed for agent=%s incident=%s", inp.agent_id, inc_hash)
    except Exception:
        logger.exception("sub_agent: agent execution error for agent=%s", inp.agent_id)
        _write_stub_to_storage(
            inp.agent_id, inp.role_name, incident_id, user_id, "failed", "agent execution error"
        )
        history = _extract_tool_call_history(tool_capture)
        _update_db_terminal(
            inp.agent_id, incident_id, user_id, org_id, "failed",
            tool_call_history=history,
        )
        return FindingRef(
            agent_id=inp.agent_id, role_name=inp.role_name,
            storage_uri=f"rca/{incident_id}/findings/{inp.agent_id}.md",
            status="failed",
            error_message="agent execution error",
            summary=f"Sub-agent {inp.agent_id} ({inp.role_name}) failed: agent execution error",
            tool_call_history=history,
        )

    final_status = _get_db_status(inp.agent_id, incident_id, user_id)
    storage_uri = f"rca/{incident_id}/findings/{inp.agent_id}.md"
    history = _extract_tool_call_history(tool_capture)

    if final_status in (None, "running"):
        logger.warning(
            "sub_agent: agent %s never called write_findings — writing stub", inp.agent_id
        )
        _write_stub_to_storage(
            inp.agent_id, inp.role_name, incident_id, user_id,
            "inconclusive", "agent completed without calling write_findings",
        )
        _update_db_terminal(
            inp.agent_id, incident_id, user_id, org_id, "inconclusive",
            tool_call_history=history,
        )
        final_status = "inconclusive"
    else:
        # Persist the tool_call_history alongside whatever write_findings wrote
        _persist_tool_call_history(inp.agent_id, incident_id, user_id, history)

    # FindingRef.status only accepts succeeded/failed/timeout/cancelled.
    # "inconclusive" and any other value collapse to "succeeded" — the body
    # itself carries the precise status in its frontmatter.
    fr_status = final_status if final_status in _FINDING_REF_STATUSES else "succeeded"

    summary_text = _read_summary_from_storage(incident_id, inp.agent_id, user_id)
    if not summary_text:
        summary_text = f"Sub-agent {inp.agent_id} ({inp.role_name}) {final_status}"

    return FindingRef(
        agent_id=inp.agent_id, role_name=inp.role_name,
        storage_uri=storage_uri,
        status=fr_status,
        summary=summary_text,
        tool_call_history=history,
    )


def _write_stub_to_storage(agent_id: str, role_name: str, incident_id: str,
                            user_id: str, status: str, error_message: str) -> None:
    try:
        stub = make_stub(
            agent_id=agent_id, role_name=role_name, incident_id=incident_id,
            purpose="see error_message", status=status, error_message=error_message,
        )
        from utils.storage.storage import get_storage_manager
        storage_uri = f"rca/{incident_id}/findings/{agent_id}.md"
        get_storage_manager(user_id).upload_bytes(
            stub.encode("utf-8"), storage_uri, user_id, content_type="text/markdown"
        )
    except Exception:
        logger.exception("sub_agent: failed to write stub for agent %s", agent_id)


def _update_db_terminal(agent_id: str, incident_id: str, user_id: str,
                         org_id: Optional[str], status: str,
                         tool_call_history: Optional[list] = None) -> None:
    from utils.db.connection_pool import db_pool
    from utils.auth.stateless_auth import set_rls_context
    import json as _json

    try:
        now = datetime.now(timezone.utc)
        history_json = _json.dumps(tool_call_history or [])
        # Always set storage_uri to the deterministic stub path. _write_stub_to_storage
        # uploaded a stub on every failure path, so the route can serve a body instead
        # of stalling the UI on body=null + terminal status.
        storage_uri = f"rca/{incident_id}/findings/{agent_id}.md"
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[SubAgent]")
                cur.execute(
                    "UPDATE rca_findings SET status=%s, completed_at=%s, "
                    "tool_call_history=%s::jsonb, storage_uri=COALESCE(storage_uri, %s) "
                    "WHERE incident_id=%s AND agent_id=%s",
                    (status, now, history_json, storage_uri, incident_id, agent_id),
                )
            conn.commit()
    except Exception:
        logger.exception("sub_agent: failed to update terminal DB row for agent %s", agent_id)


def _persist_tool_call_history(agent_id: str, incident_id: str, user_id: str,
                                tool_call_history: list) -> None:
    """Write tool_call_history into rca_findings without touching status/completed_at."""
    from utils.db.connection_pool import db_pool
    from utils.auth.stateless_auth import set_rls_context
    import json as _json

    try:
        history_json = _json.dumps(tool_call_history or [])
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[SubAgent:hist]")
                cur.execute(
                    "UPDATE rca_findings SET tool_call_history=%s::jsonb "
                    "WHERE incident_id=%s AND agent_id=%s",
                    (history_json, incident_id, agent_id),
                )
            conn.commit()
    except Exception:
        logger.exception(
            "sub_agent: failed to persist tool_call_history for agent %s", agent_id
        )


def _get_db_status(agent_id: str, incident_id: str, user_id: str) -> Optional[str]:
    from utils.db.connection_pool import db_pool
    from utils.auth.stateless_auth import set_rls_context

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[SubAgent:status]")
                cur.execute(
                    "SELECT status FROM rca_findings WHERE incident_id=%s AND agent_id=%s",
                    (incident_id, agent_id),
                )
                row = cur.fetchone()
                return row[0] if row else None
    except Exception:
        logger.exception("sub_agent: failed to read DB status for agent %s", agent_id)
        return None
