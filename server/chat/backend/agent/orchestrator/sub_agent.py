"""Sub-agent node: runs one bounded ReAct investigation and writes findings.md."""

import asyncio
import copy
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from chat.backend.agent.orchestrator.inputs import FindingRef, SubAgentInput
from chat.backend.agent.orchestrator.findings_schema import make_stub
from chat.backend.agent.utils.tool_call_history import (
    MAX_HISTORY_ENTRIES,
    OUTPUT_EXCERPT_MAX_CHARS,
    derive_command,
)
from utils.log_sanitizer import hash_for_log
from utils.text.text_utils import truncate

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 600
_FINDING_REF_STATUSES = frozenset({"succeeded", "failed", "timeout", "cancelled", "inconclusive"})

# Loop-guard thresholds: number of consecutive empty/error results from the
# same tool before we append a soft/hard nudge to the tool output. The agent
# has been trained to follow these structured warnings; we never break the
# StructuredTool contract — the original output is preserved, we just append.
_LOOP_GUARD_SOFT_THRESHOLD = 3
_LOOP_GUARD_HARD_THRESHOLD = 5


def _classify_tool_output_empty(output) -> bool:
    """Return True if the tool output looks empty/error-y.

    Defensive: any parse error returns False (treat as non-empty).
    """
    try:
        text = output if isinstance(output, str) else str(output)
        if not text or not text.strip():
            return True
        try:
            data = json.loads(text)
        except Exception:
            return False
        if not isinstance(data, dict):
            return False
        if data.get("success") is False:
            return True
        if data.get("error"):
            return True
        # query_datadog / similar empty-series shape
        if data.get("count", -1) == 0:
            return True
        results = data.get("results")
        if isinstance(results, list) and len(results) == 0:
            return True
        series = data.get("series")
        if isinstance(series, list) and len(series) == 0:
            return True
        return False
    except Exception:
        return False


def _build_loop_guard_warning(tool_name: str, n: int) -> str:
    if n >= _LOOP_GUARD_HARD_THRESHOLD:
        return (
            f"\n\n[LOOP-GUARD] {n} consecutive empty/error results from "
            f"{tool_name}. STOP retrying this source — your next call MUST "
            "be write_findings. Document what you ruled out and submit "
            "findings now."
        )
    return (
        f"\n\n[LOOP-GUARD] {n} consecutive empty/error results from "
        f"{tool_name}. Stop trying the same source — pivot to a different "
        "tool or call write_findings."
    )


def _wrap_tool_with_loop_guard(tool, counters: dict):
    """Append a [LOOP-GUARD] suffix to tool output after N consecutive
    empty/error results from the same tool name.

    Never wraps `write_findings` (the terminal call). Returns a shallow copy
    of the StructuredTool so we don't mutate the cached/shared instances
    returned by `get_cloud_tools()`.
    """
    if getattr(tool, "name", None) == "write_findings":
        return tool

    try:
        wrapped = copy.copy(tool)
    except Exception:
        # If copy fails for any reason, fall back to the original tool
        # un-wrapped rather than risk breaking the agent.
        logger.debug("sub_agent: copy.copy(tool) failed for %s — skipping loop guard", getattr(tool, "name", "?"), exc_info=True)
        return tool

    tool_name = getattr(wrapped, "name", "unknown_tool")
    original_coroutine = getattr(wrapped, "coroutine", None)
    original_func = getattr(wrapped, "func", None)

    def _post_process(output):
        try:
            is_empty = _classify_tool_output_empty(output)
            if is_empty:
                counters[tool_name] = counters.get(tool_name, 0) + 1
            else:
                counters[tool_name] = 0
            n = counters.get(tool_name, 0)
            if n >= _LOOP_GUARD_SOFT_THRESHOLD and isinstance(output, str):
                return output + _build_loop_guard_warning(tool_name, n)
            return output
        except Exception:
            logger.debug("sub_agent: loop-guard post-process failed for %s", tool_name, exc_info=True)
            return output

    if original_coroutine is not None:
        async def wrapped_coroutine(*args, **kwargs):
            result = await original_coroutine(*args, **kwargs)
            return _post_process(result)
        wrapped.coroutine = wrapped_coroutine

    if original_func is not None:
        def wrapped_func(*args, **kwargs):
            result = original_func(*args, **kwargs)
            return _post_process(result)
        wrapped.func = wrapped_func

    return wrapped


def _serialize_args(value, limit: int = OUTPUT_EXCERPT_MAX_CHARS) -> str:
    """JSON-encode tool args so downstream consumers can json.loads without
    needing a Python-repr fallback. Falls back to str() for non-serializable
    values (rare; keeps the field non-empty)."""
    if value is None:
        return ""
    if isinstance(value, str):
        s = value
    else:
        try:
            s = json.dumps(value, default=str)
        except (TypeError, ValueError):
            s = str(value)
    return s if len(s) <= limit else s[:limit] + "...[truncated]"


def _extract_tool_call_history(tool_capture) -> list[dict]:
    """Serialize ToolContextCapture's per-session tool calls as a small list.

    Reads from `tool_history` (append-only, survives GC) primarily; falls back
    to `current_tool_calls` for any in-flight (not-yet-completed) entries so a
    timeout still surfaces calls that hadn't finished.

    Best-effort: anything unexpected returns an empty list rather than raising.
    """
    if tool_capture is None:
        return []
    try:
        items: list[dict] = []
        seen_ids: set = set()

        history = getattr(tool_capture, "tool_history", []) or []
        for entry in history:
            if not isinstance(entry, dict):
                continue
            call_id = entry.get("tool_call_id")
            if call_id and call_id in seen_ids:
                continue
            if call_id:
                seen_ids.add(call_id)
            input_dict = entry.get("input")
            items.append({
                "tool_name": truncate(entry.get("tool_name") or "unknown", 128),
                "args": _serialize_args(input_dict),
                "command": derive_command(input_dict),
                "output_excerpt": truncate(entry.get("output_excerpt") or "", OUTPUT_EXCERPT_MAX_CHARS),
                "is_error": bool(entry.get("is_error", False)),
                "status": "error" if entry.get("is_error", False) else "completed",
                "started_at": entry.get("started_at"),
                "completed_at": entry.get("completed_at"),
            })

        # Pull in any still-running calls that never completed (e.g. timeout).
        raw = getattr(tool_capture, "current_tool_calls", {}) or {}
        for call_id, info in raw.items():
            if not isinstance(info, dict) or call_id in seen_ids:
                continue
            started = info.get("start_time")
            try:
                started_iso = started.isoformat() if started else None
            except Exception:
                started_iso = None
            input_dict = info.get("input")
            items.append({
                "tool_name": truncate(info.get("tool_name") or "unknown", 128),
                "args": _serialize_args(input_dict),
                "command": derive_command(input_dict),
                "output_excerpt": "",
                "is_error": False,
                "status": "running",
                "started_at": started_iso,
                "completed_at": None,
            })

        try:
            items.sort(key=lambda d: d.get("started_at") or "")
        except Exception:
            logger.debug("sub_agent: tool_history sort skipped due to malformed entry", exc_info=True)
        return items[:MAX_HISTORY_ENTRIES]
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
            logger.debug("sub_agent: invalid wave value, leaving ref.wave unset", exc_info=True)

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
        logger.debug("sub_agent: role_meta lookup failed, using default timeout %ds", _DEFAULT_TIMEOUT_SECONDS, exc_info=True)

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
        # Narrow race: _run could have completed write_findings (storage upload +
        # DB row to terminal status) just before the timeout deadline. Don't
        # clobber a real findings.md with a stub in that case.
        existing_status = await asyncio.to_thread(
            _get_db_status, agent_id, incident_id, user_id
        )
        if existing_status in (None, "running"):
            await asyncio.to_thread(
                _write_stub_to_storage, agent_id, role_name, incident_id, user_id,
                "timeout", "timed out",
            )
            await asyncio.to_thread(
                _update_db_terminal, agent_id, incident_id, user_id, "timeout",
                tool_call_history=history,
            )
            terminal_status = "timeout"
        else:
            # Already terminal; persist the partial history alongside whatever
            # write_findings wrote, but don't downgrade the row.
            await asyncio.to_thread(
                _persist_tool_call_history, agent_id, incident_id, user_id, history
            )
            terminal_status = existing_status
        if terminal_status == "timeout":
            summary = f"Sub-agent {agent_id} ({role_name}) timed out after {timeout}s"
        else:
            summary = f"Sub-agent {agent_id} ({role_name}) completed with status {terminal_status}"
        return FindingRef(
            agent_id=agent_id, role_name=role_name,
            storage_uri=f"rca/{incident_id}/findings/{agent_id}.md",
            status=terminal_status if terminal_status in _FINDING_REF_STATUSES else "timeout",
            summary=summary,
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
        logger.exception("sub_agent: SubAgentInput validation failed")
        agent_id = input_dict.get("agent_id", "unknown")
        role_name = input_dict.get("role_name", "")
        incident_id = input_dict.get("parent_incident_id", "")
        user_id = input_dict.get("parent_user_id", "")
        if incident_id and user_id:
            await asyncio.to_thread(
                _write_stub_to_storage, agent_id, role_name, incident_id, user_id,
                "failed", f"input validation: {exc}",
            )
            await asyncio.to_thread(
                _update_db_terminal, agent_id, incident_id, user_id, "failed",
                tool_call_history=[],
            )
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
    child_session_id = f"{parent_session_id}::{inp.agent_id}"
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
    from chat.backend.agent.llm import ModelConfig
    role_meta = RoleRegistry.get_instance().get(inp.role_name)
    if not role_meta:
        logger.error("sub_agent: role %r not found in registry", inp.role_name)
        if incident_id and user_id:
            await asyncio.to_thread(
                _write_stub_to_storage, inp.agent_id, inp.role_name, incident_id, user_id,
                "failed", f"role {inp.role_name!r} not found",
            )
            await asyncio.to_thread(
                _update_db_terminal, inp.agent_id, incident_id, user_id, "failed",
                tool_call_history=[],
            )
        return FindingRef(
            agent_id=inp.agent_id, role_name=inp.role_name,
            storage_uri=f"rca/{incident_id}/findings/{inp.agent_id}.md" if incident_id else None,
            status="failed",
            error_message=f"role {inp.role_name!r} not found",
        )

    sub_agent_model = role_meta.model or ModelConfig.RCA_SUBAGENT_MODEL
    if not sub_agent_model:
        err = (
            "RCA_SUBAGENT_MODEL must be set when ORCHESTRATOR_ENABLED=true "
            "(or set `model:` frontmatter on the role)"
        )
        logger.error("sub_agent: %s — role=%r agent=%s", err, inp.role_name, inp.agent_id)
        if incident_id and user_id:
            await asyncio.to_thread(
                _write_stub_to_storage, inp.agent_id, inp.role_name, incident_id, user_id,
                "failed", err,
            )
            await asyncio.to_thread(
                _update_db_terminal, inp.agent_id, incident_id, user_id, "failed",
                tool_call_history=[],
            )
        return FindingRef(
            agent_id=inp.agent_id, role_name=inp.role_name,
            storage_uri=f"rca/{incident_id}/findings/{inp.agent_id}.md" if incident_id else None,
            status="failed",
            error_message=err,
        )

    from chat.backend.agent.orchestrator.inputs import render_brief
    from chat.backend.agent.orchestrator.select_skills import (
        load_skills_for_role,
        select_tools_for_role,
    )
    from chat.backend.agent.orchestrator.findings_writer import make_write_findings_tool
    from chat.backend.agent.tools.cloud_tools import get_cloud_tools

    connected_providers: list[str] = []
    try:
        from chat.background.rca_prompt_builder import get_user_providers
        connected_providers = get_user_providers(user_id)
    except Exception:
        logger.exception("sub_agent: failed to resolve connected providers for %s", inp.agent_id)

    brief = render_brief(inp, role_meta, connected_providers=connected_providers)
    skill_content = load_skills_for_role(user_id, role_meta)
    if skill_content:
        brief = brief + "\n\n## Integration-Specific Guidance\n\n" + skill_content
    all_tools = get_cloud_tools()
    role_tools = select_tools_for_role(user_id, role_meta, all_tools)
    write_tool = make_write_findings_tool(
        agent_id=inp.agent_id, role_name=inp.role_name,
        incident_id=incident_id, user_id=user_id,
        child_session_id=child_session_id,
    )
    tools = role_tools + [write_tool]

    # Per-sub-agent loop guard: track consecutive empty/error results per tool
    # name and append a structured warning to subsequent outputs once we cross
    # the soft/hard thresholds. write_findings is excluded inside the wrapper.
    _loop_counters: dict = {}
    tools = [_wrap_tool_with_loop_guard(t, _loop_counters) for t in tools]

    try:
        from chat.backend.agent.agent import Agent
        from chat.backend.agent.db import PostgreSQLClient
        from chat.backend.agent.weaviate_client import WeaviateClient
        from chat.backend.agent.utils.state import State
        from langchain_core.messages import HumanMessage

        kickoff = (
            "Begin your investigation now. Use the tools available to gather "
            "evidence, then call `write_findings` exactly once with your final "
            "findings.md body. Do not respond with plain text — every reply "
            "must be either a tool call or the terminating `write_findings` call."
        )
        sub_state = State(
            question=kickoff,
            messages=[HumanMessage(content=kickoff)],
            user_id=user_id,
            session_id=child_session_id,
            incident_id=incident_id,
            incident_start_time=input_dict.get("parent_incident_start_time"),
            org_id=org_id,
            is_background=True,
            mode="ask",
            model=sub_agent_model,
        )

        postgres_client = PostgreSQLClient()
        weaviate_client = WeaviateClient(postgres_client)
        try:
            agent = Agent(
                weaviate_client=weaviate_client,
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
        finally:
            try:
                weaviate_client.close()
            except Exception:
                logger.exception("sub_agent: failed to close weaviate_client for agent=%s", inp.agent_id)
            try:
                postgres_client.close()
            except Exception:
                logger.exception("sub_agent: failed to close postgres_client for agent=%s", inp.agent_id)
    except Exception:
        logger.exception("sub_agent: agent execution error for agent=%s", inp.agent_id)
        await asyncio.to_thread(
            _write_stub_to_storage,
            inp.agent_id, inp.role_name, incident_id, user_id, "failed", "agent execution error",
        )
        history = _extract_tool_call_history(tool_capture)
        await asyncio.to_thread(
            _update_db_terminal,
            inp.agent_id, incident_id, user_id, "failed",
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

    final_status = await asyncio.to_thread(_get_db_status, inp.agent_id, incident_id, user_id)
    storage_uri = f"rca/{incident_id}/findings/{inp.agent_id}.md"
    history = _extract_tool_call_history(tool_capture)

    if final_status in (None, "running"):
        logger.warning(
            "sub_agent: agent %s never called write_findings — writing stub", inp.agent_id
        )
        await asyncio.to_thread(
            _write_stub_to_storage,
            inp.agent_id, inp.role_name, incident_id, user_id,
            "inconclusive", "agent completed without calling write_findings",
        )
        await asyncio.to_thread(
            _update_db_terminal,
            inp.agent_id, incident_id, user_id, "inconclusive",
            tool_call_history=history,
        )
        final_status = "inconclusive"
    else:
        # Persist the tool_call_history alongside whatever write_findings wrote
        await asyncio.to_thread(
            _persist_tool_call_history, inp.agent_id, incident_id, user_id, history
        )

    # Map any unexpected DB value to "failed" so the FindingRef Literal stays valid.
    fr_status = final_status if final_status in _FINDING_REF_STATUSES else "failed"

    summary_text = await asyncio.to_thread(
        _read_summary_from_storage, incident_id, inp.agent_id, user_id
    )
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
                         status: str,
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
                if set_rls_context(cur, conn, user_id, log_prefix="[SubAgent]") is None:
                    logger.warning(
                        "sub_agent: failed to set RLS context for terminal update agent=%s",
                        agent_id,
                    )
                    return
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
                if set_rls_context(cur, conn, user_id, log_prefix="[SubAgent:hist]") is None:
                    logger.warning(
                        "sub_agent: failed to set RLS context for history persist agent=%s",
                        agent_id,
                    )
                    return
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
                if set_rls_context(cur, conn, user_id, log_prefix="[SubAgent:status]") is None:
                    logger.warning(
                        "sub_agent: failed to set RLS context for status read agent=%s",
                        agent_id,
                    )
                    return None
                cur.execute(
                    "SELECT status FROM rca_findings WHERE incident_id=%s AND agent_id=%s",
                    (incident_id, agent_id),
                )
                row = cur.fetchone()
                return row[0] if row else None
    except Exception:
        logger.exception("sub_agent: failed to read DB status for agent %s", agent_id)
        return None
