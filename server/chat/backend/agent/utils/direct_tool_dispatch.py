"""Transport-neutral direct-tool dispatcher.

Both the WS path (``main_chatbot.handle_connection``) and the SSE control
listener route a direct tool call (``github_commit`` / ``iac_tool`` today)
through ``dispatch_direct_tool_call``. Returns a ``DirectToolOutcome`` the
caller can serialize onto its own transport (websocket frame, chat_event,
HTTP body, etc.).

This module owns:
  * ModeAccessController gating
  * ``set_user_context`` for the tool
  * Tool execution
  * Emitting the canonical ``tool_call_result`` chat_event so SSE clients
    see the outcome regardless of transport
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class DirectToolOutcome:
    status: str  # "ok" | "denied" | "error"
    tool_name: str
    result: Any = None
    error: Optional[str] = None
    code: Optional[str] = None  # e.g. "READ_ONLY_MODE"


class _MockState:
    """Minimal state shim — direct tools only need session_id from state."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id


async def dispatch_direct_tool_call(
    *,
    payload: Dict[str, Any],
    user_id: str,
    session_id: str,
    mode: str = "agent",
    provider_preference: Optional[list] = None,
    selected_project_id: Optional[str] = None,
) -> DirectToolOutcome:
    """Dispatch a direct tool call. Transport-neutral.

    ``payload`` is the dict the client sends, e.g.
        {"tool_name": "github_commit", "parameters": {...}}
    """
    from chat.backend.agent.access import ModeAccessController
    from chat.backend.agent.tools.cloud_tools import set_user_context

    tool_name = payload.get("tool_name")
    parameters = payload.get("parameters") or {}
    if not tool_name:
        return DirectToolOutcome(status="error", tool_name="", error="tool_name required")

    mock_state = _MockState(session_id)
    set_user_context(
        user_id=user_id,
        session_id=session_id,
        provider_preference=provider_preference,
        selected_project_id=selected_project_id,
        state=mock_state,
        mode=mode,
    )

    if tool_name == "github_commit":
        if not ModeAccessController.is_tool_allowed(mode, tool_name):
            return DirectToolOutcome(
                status="denied",
                tool_name=tool_name,
                error="github_commit is not available in Ask mode. Switch to Agent mode to push changes.",
                code="READ_ONLY_MODE",
            )
        repo = parameters.get("repo")
        commit_message = parameters.get("commit_message")
        if not repo or not commit_message:
            return DirectToolOutcome(
                status="error",
                tool_name=tool_name,
                error="github_commit requires non-empty 'repo' and 'commit_message'",
            )
        try:
            from chat.backend.agent.tools.github_commit_tool import github_commit
            result = await asyncio.to_thread(
                github_commit,
                repo=repo,
                commit_message=commit_message,
                branch=parameters.get("branch", "main"),
                push=parameters.get("push", True),
                user_id=user_id,
                session_id=session_id,
            )
        except Exception as e:
            logger.error("dispatch_direct_tool_call: %s failed: %s", tool_name, e)
            return DirectToolOutcome(status="error", tool_name=tool_name, error=str(e))
        await _emit_tool_call_result(session_id, user_id, tool_name, result)
        return DirectToolOutcome(status="ok", tool_name=tool_name, result=result)

    if tool_name == "iac_tool":
        action = parameters.get("action", "write")
        is_allowed, denial = ModeAccessController.ensure_iac_action_allowed(mode, action)
        if not is_allowed:
            return DirectToolOutcome(
                status="denied",
                tool_name=tool_name,
                error=denial,
                code="READ_ONLY_MODE",
            )
        try:
            from chat.backend.agent.tools.iac_tool import run_iac_tool
            result = await asyncio.to_thread(
                run_iac_tool,
                action=action,
                path=parameters.get("path"),
                content=parameters.get("content"),
                directory=parameters.get("directory"),
                vars=parameters.get("vars"),
                auto_approve=parameters.get("auto_approve"),
                user_id=user_id,
                session_id=session_id,
            )
        except Exception as e:
            logger.error("dispatch_direct_tool_call: %s failed: %s", tool_name, e)
            return DirectToolOutcome(status="error", tool_name=tool_name, error=str(e))
        await _emit_tool_call_result(session_id, user_id, tool_name, result)
        return DirectToolOutcome(status="ok", tool_name=tool_name, result=result)

    return DirectToolOutcome(
        status="error",
        tool_name=tool_name,
        error=f"unsupported direct tool: {tool_name}",
    )


async def _emit_tool_call_result(
    session_id: str, user_id: str, tool_name: str, result: Any
) -> None:
    """Emit the SSE-visible ``tool_call_result`` chat_event for a direct tool run."""
    try:
        from chat.backend.agent.utils.persistence.chat_events import record_event
        from utils.auth.stateless_auth import get_org_id_for_user

        # get_org_id_for_user is a sync DB lookup; offload off the event loop.
        org_id = await asyncio.to_thread(get_org_id_for_user, user_id)
        if not org_id or not session_id:
            logger.debug(
                "[direct_tool_dispatch] skipping tool_call_result emit "
                "(org_id=%s session_id=%s user=%s)",
                bool(org_id), bool(session_id), user_id,
            )
            return
        await record_event(
            session_id=session_id,
            org_id=org_id,
            type="tool_call_result",
            payload={"tool_name": tool_name, "result": result, "status": "complete"},
        )
    except Exception as e:
        logger.warning("[chat_events:dual_write_failed] %s", e)
