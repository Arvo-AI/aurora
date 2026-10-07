"""LangChain tools backed by customer-registered MCP servers.

Separate from ``mcp_tools.py`` on purpose: that module builds tools for the
three built-in servers and drops anything it does not recognise, and its tool
classification is a denylist tuned to those servers' naming. Customer servers
need the inverted rule (read-prefix allowlist) and must never be silently
dropped.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any, Callable, Dict, List, Optional

from langchain_core.tools import StructuredTool

from chat.backend.agent.tools.mcp_schema_extractor import extract_mcp_tool_schema
from chat.backend.agent.utils.tool_output_cap import cap_tool_output
from connectors.mcp_connector.client import MCPConnectionError, call
from connectors.mcp_connector.store import (
    TOOL_NAME_RE,
    is_read_tool,
    list_servers,
    qualified_tool_name,
    tool_mode,
    update_auth,
)
from utils.auth.command_gate import gate_action
from utils.cloud.cloud_utils import get_user_context
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)


def is_mcp_connected(user_id: str) -> bool:
    """True when the org has at least one registered MCP server."""
    return bool(list_servers(user_id))


def _tool_allowed(
    server: Dict[str, Any], tool: Dict[str, Any], is_background: bool, is_pr_review: bool
) -> bool:
    """Whether this tool may be offered to the agent in the current context.

    The user's per-tool override wins over everything -- ``never`` hides a tool
    the classifier called safe, and ``always`` means always, including during
    RCA and PR review where nothing else can offer a write.

    Otherwise: reads are always offered; writes only in foreground chat, where
    ``gate_action`` can ask a human. In background there is nobody to approve,
    so a write is withheld rather than offered and then denied mid-investigation.

    ``read_only`` is enforced here rather than at registration, so flipping it
    takes effect on the next turn without re-probing the server.
    """
    mode = tool_mode(server, tool.get("name", ""))
    if mode == "never":
        return False
    if mode == "always":
        return True
    if is_read_tool(tool):
        return True
    if server.get("read_only", True):
        return False
    return not (is_background or is_pr_review)


def _summarize(label: str, tool_name: str, kwargs: Dict[str, Any]) -> str:
    """Human-readable confirmation prompt for a write tool."""
    try:
        args = json.dumps(kwargs, sort_keys=True, default=str)[:500]
    except Exception:
        args = "(unserializable arguments)"
    return (
        f"The MCP tool '{tool_name}' on your '{label}' server will run with "
        f"arguments: {args}\n\n"
        "Aurora cannot verify what a third-party tool changes.\n\n"
    )


def _make_wrapper(
    server: Dict[str, Any],
    tool_name: str,
    public_name: str,
    needs_gate: bool,
    user_id: str,
    send_tool_start: Optional[Callable] = None,
    send_tool_completion: Optional[Callable] = None,
    send_tool_error: Optional[Callable] = None,
    run_async_in_thread: Optional[Callable] = None,
) -> Callable[..., str]:
    """Build the sync callable LangChain invokes for one remote MCP tool."""
    label = server["label"]
    url, auth, transport = server["url"], server.get("auth"), server.get("transport", "streamable_http")

    def _persist_refresh(new_auth: Dict[str, Any]) -> None:
        """Store a rotated OAuth token so the next call does not refresh again.

        Best-effort: the tool call already succeeded with the new token, so a
        storage failure must not fail the call. It only costs another refresh.
        """
        try:
            update_auth(user_id, label, new_auth)
        except Exception as exc:
            logger.warning(
                "[MCP] Could not persist refreshed token for '%s': %s",
                sanitize(label), sanitize(exc),
            )

    def wrapper(**kwargs: Any) -> str:
        signature = f"{public_name}_{json.dumps(kwargs, sort_keys=True, default=str)}"
        tool_call_id = f"{public_name}_{hashlib.sha256(signature.encode()).hexdigest()[:16]}"
        if send_tool_start:
            try:
                send_tool_start(public_name, kwargs, tool_call_id)
            except Exception as exc:
                logger.warning("Failed to send start notification for %s: %s", public_name, exc)

        if needs_gate:
            context = get_user_context()
            gate_user = context.get("user_id") if isinstance(context, dict) else context
            decision = gate_action(
                user_id=gate_user,
                tool_name=public_name,
                summary=_summarize(label, tool_name, kwargs),
            )
            if not decision.allowed:
                message = f"MCP tool {tool_name} was not approved."
                if send_tool_completion:
                    try:
                        send_tool_completion(public_name, message, "cancelled", tool_call_id)
                    except Exception:
                        pass
                return message

        args = {k: v for k, v in kwargs.items() if v is not None}
        try:
            coro = call(url, auth, transport, tool_name, args, on_refresh=_persist_refresh)
            result = (
                run_async_in_thread(coro) if run_async_in_thread else asyncio.run(coro)
            )
        except MCPConnectionError as exc:
            message = f"MCP server '{label}' error: {exc}"
            logger.warning("[MCP] %s", sanitize(message))
            if send_tool_error:
                try:
                    send_tool_error(public_name, str(exc))
                except Exception:
                    pass
            return message
        except Exception as exc:
            logger.exception("[MCP] Unexpected failure calling %s", sanitize(public_name))
            if send_tool_error:
                try:
                    send_tool_error(public_name, str(exc))
                except Exception:
                    pass
            return f"Error calling {tool_name} on '{label}': {exc}"

        text = cap_tool_output(result if isinstance(result, str) else str(result), public_name)
        if send_tool_completion:
            try:
                send_tool_completion(public_name, text, "completed", tool_call_id)
            except Exception as exc:
                logger.warning("Failed to send completion for %s: %s", public_name, exc)
        return text

    return wrapper


def get_custom_mcp_tools(
    user_id: str,
    is_background: bool = False,
    is_pr_review: bool = False,
    tool_capture: Any = None,
    send_tool_start: Optional[Callable] = None,
    send_tool_completion: Optional[Callable] = None,
    send_tool_error: Optional[Callable] = None,
    run_async_in_thread: Optional[Callable] = None,
    wrap_func_with_capture: Optional[Callable] = None,
) -> List[StructuredTool]:
    """Build StructuredTools for every registered MCP server's cached tools.

    Reads cached schemas from Vault -- no network calls here, so assembling the
    agent's toolset costs one secret read regardless of server count.
    """
    tools: List[StructuredTool] = []
    seen: set = set()

    for server in list_servers(user_id):
        label = server["label"]
        for tool_def in server.get("tools") or []:
            tool_name = tool_def.get("name", "")
            if not tool_name or not _tool_allowed(server, tool_def, is_background, is_pr_review):
                continue

            public_name = qualified_tool_name(label, tool_name)
            if public_name in seen or not TOOL_NAME_RE.match(public_name):
                logger.warning(
                    "[MCP] Skipping tool '%s' on '%s': name collision or illegal characters",
                    sanitize(tool_name), sanitize(label),
                )
                continue
            seen.add(public_name)

            func = _make_wrapper(
                server,
                tool_name,
                public_name,
                needs_gate=not is_read_tool(tool_def)
                and tool_mode(server, tool_name) != "always",
                user_id=user_id,
                send_tool_start=send_tool_start,
                send_tool_completion=send_tool_completion,
                send_tool_error=send_tool_error,
                run_async_in_thread=run_async_in_thread,
            )
            if tool_capture and wrap_func_with_capture:
                func = wrap_func_with_capture(func, public_name)

            description = (tool_def.get("description") or f"Tool {tool_name}").strip()
            kwargs: Dict[str, Any] = {
                "func": func,
                "name": public_name,
                "description": f"[MCP:{label}] {description}",
                # Ask mode blocks the whole `mcp_` prefix because a bare tool name
                # says nothing about what the tool does. Carry the classification
                # we already computed so reads survive the filter.
                "metadata": {"mcp_read_only": is_read_tool(tool_def)},
            }
            schema = extract_mcp_tool_schema(tool_def)
            if schema:
                kwargs["args_schema"] = schema
            tools.append(StructuredTool.from_function(**kwargs))

    if tools:
        logger.info(
            "Added %d custom MCP tools for user %s (background=%s, pr_review=%s)",
            len(tools), sanitize(user_id), is_background, is_pr_review,
        )
    return tools
