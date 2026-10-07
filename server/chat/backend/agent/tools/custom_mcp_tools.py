"""Agent access to customer-registered MCP servers, via two dispatcher tools.

Separate from ``mcp_tools.py`` on purpose: that module builds tools for the
three built-in servers and drops anything it does not recognise, and its tool
classification is a denylist tuned to those servers' naming. Customer servers
need the inverted rule (read-prefix allowlist) and must never be silently
dropped.

Only ``mcp_list_tools`` and ``mcp_call_tool`` are registered with the agent,
whatever the user has connected. One StructuredTool per remote tool meant every
name, description and JSON schema entered the prompt on every turn, so three
servers with 150 tools each cost ~160k tokens before the user typed anything.
Same shape as ``get_connected_clusters`` then ``kubectl``: discover, then call.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any, Callable, Dict, List, Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from chat.backend.agent.access.mode_access_controller import ModeAccessController
from chat.backend.agent.utils.tool_output_cap import cap_tool_output
from connectors.mcp_connector.client import MCPConnectionError, call
from connectors.mcp_connector.store import (
    is_read_tool,
    list_servers,
    qualified_tool_name,
    slugify_label,
    tool_mode,
    update_auth,
)
from utils.auth.command_gate import gate_action
from utils.cloud.cloud_utils import get_user_context
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

# Discovery response caps. A server can expose hundreds of tools; returning all
# of them with full JSON Schemas would move the context blowup from the prompt
# into the tool result. Schemas are 80% of a listing's bytes (25 Linear tools:
# 30KB of 38KB), so listings carry a compact arg summary and the full schema is
# only ever returned for a single named tool.
DEFAULT_LIST_LIMIT = 25
MAX_LIST_LIMIT = 100
LIST_DESCRIPTION_CHARS = 200


class McpListToolsArgs(BaseModel):
    server: Optional[str] = Field(
        default=None, description="Server label from a bare call. Omit for the server list."
    )
    tool: Optional[str] = Field(
        default=None, description="Tool name, to get its full input schema before calling it."
    )
    query: Optional[str] = Field(
        default=None, description="Substring to match against tool names and descriptions."
    )
    limit: int = Field(default=DEFAULT_LIST_LIMIT, description="Max tools to return (1-100).")


class McpCallToolArgs(BaseModel):
    server: str = Field(description="Server label exactly as mcp_list_tools returned it.")
    tool: str = Field(description="Tool name exactly as mcp_list_tools returned it.")
    arguments: Optional[Dict[str, Any]] = Field(
        default=None, description="Arguments matching the tool's inputSchema."
    )


def is_mcp_connected(user_id: str) -> bool:
    """True when the org has at least one registered MCP server."""
    return bool(list_servers(user_id))


def _tool_allowed(
    server: Dict[str, Any], tool: Dict[str, Any], is_background: bool, is_pr_review: bool
) -> bool:
    """Whether this tool may be offered to the agent in the current context.

    ``allow`` runs anywhere. ``confirm`` runs only in foreground chat, where
    ``gate_action`` can ask a human; in background there is nobody to approve,
    so it is withheld rather than offered and then denied mid-investigation.
    """
    if tool_mode(server, tool) == "allow":
        return True
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


def _call_tool(
    user_id: str,
    server_label: str,
    tool: str,
    arguments: Optional[Dict[str, Any]],
    is_background: bool,
    is_pr_review: bool,
    mode: Optional[str],
    send_tool_start: Optional[Callable] = None,
    send_tool_completion: Optional[Callable] = None,
    send_tool_error: Optional[Callable] = None,
    run_async_in_thread: Optional[Callable] = None,
) -> str:
    """Resolve a (server, tool) pair and invoke it, or refuse with a reason.

    This is the security boundary. When each remote tool was its own
    StructuredTool, a tool the user had disabled simply was not built and the
    model could not name it. The model can now name anything, so every one of
    those rules is re-checked here. ``_visible`` is the same function discovery
    uses, so "not listed" and "not callable" cannot drift apart.

    Refusals are returned, not raised: the agent reads the reason and adapts,
    where an exception would surface as an opaque tool failure.
    """
    target = slugify_label(server_label)
    pairs = _visible(user_id, is_background, is_pr_review, mode)

    match = next((p for p in pairs if p[0]["label"] == target and p[1]["name"] == tool), None)
    if match is None:
        # Distinguish "does not exist" from "exists but is not available to you
        # right now" -- otherwise the agent retries a tool that will never work.
        for s in list_servers(user_id):
            if s["label"] != target:
                continue
            found = next((t for t in s.get("tools") or [] if t.get("name") == tool), None)
            if found is None:
                continue
            if ModeAccessController.is_read_only_mode(mode):
                return json.dumps({
                    "error": "read_only_mode",
                    "detail": f"'{tool}' writes, and Ask mode is read-only. Switch to Agent mode.",
                })
            return json.dumps({
                "error": "needs_confirmation_unavailable",
                "detail": (
                    f"'{tool}' requires user confirmation and nobody can approve it "
                    "during an automated investigation. Report the intended action instead."
                ),
            })
        known = sorted({s["label"] for s, _ in pairs})
        return json.dumps({
            "error": "unknown_tool",
            "detail": f"No tool '{tool}' on server '{server_label}'.",
            "known_servers": known,
            "hint": "Use mcp_list_tools(server=...) for exact names.",
        })

    srv, tool_def = match
    args = dict(arguments or {})

    # The per-tool args_schema that LangChain used to validate against is gone,
    # so catch a missing required key here. Cheaper and clearer than letting the
    # remote server answer 400 with its own wording.
    required = (tool_def.get("inputSchema") or {}).get("required") or []
    missing = [k for k in required if k not in args]
    if missing:
        return json.dumps({
            "error": "missing_arguments",
            "missing": missing,
            "inputSchema": tool_def.get("inputSchema") or {},
        })

    return _make_wrapper(
        srv,
        tool,
        qualified_tool_name(srv["label"], tool),
        needs_gate=tool_mode(srv, tool_def) == "confirm",
        user_id=user_id,
        send_tool_start=send_tool_start,
        send_tool_completion=send_tool_completion,
        send_tool_error=send_tool_error,
        run_async_in_thread=run_async_in_thread,
    )(**args)


def _visible(
    user_id: str, is_background: bool, is_pr_review: bool, mode: Optional[str] = None
) -> List[tuple]:
    """Every (server, tool) pair the agent may use right now.

    One source of truth for both discovery and invocation. If these diverged,
    ``mcp_list_tools`` would advertise tools ``mcp_call_tool`` then refuses --
    the agent would retry, burn turns, and report a capability that is not real.
    """
    out: List[tuple] = []
    for server in list_servers(user_id):
        for tool_def in server.get("tools") or []:
            if not tool_def.get("name"):
                continue
            if not _tool_allowed(server, tool_def, is_background, is_pr_review):
                continue
            # Ask mode is read-only. Previously enforced by name in
            # ModeAccessController.filter_tools, which cannot see individual
            # tools now that only the two dispatchers are registered.
            if ModeAccessController.is_read_only_mode(mode) and not is_read_tool(tool_def):
                continue
            out.append((server, tool_def))
    return out


def _matches(tool_def: Dict[str, Any], query: str) -> bool:
    """Substring match on name and description."""
    q = query.lower()
    return q in tool_def.get("name", "").lower() or q in (tool_def.get("description") or "").lower()


def _arg_summary(tool_def: Dict[str, Any]) -> List[str]:
    """Argument names with types and required markers, instead of raw JSON Schema.

    Full schemas dominate a listing: 25 Linear tools cost 38KB, of which 30KB
    was inputSchema, one tool contributing 8KB by itself. That recreated inside
    a tool result the same blowup this indirection removes from the prompt.
    The agent gets the full schema from ``mcp_list_tools(server=, tool=)`` or
    from the missing_arguments error, both of which cover a single tool.
    """
    schema = tool_def.get("inputSchema") or {}
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    out: List[str] = []
    for name, spec in properties.items():
        kind = (spec or {}).get("type") or "any"
        out.append(f"{name}:{kind}{'' if name in required else '?'}")
    return out


def _shape(server: Dict[str, Any], tool_def: Dict[str, Any]) -> Dict[str, Any]:
    """One tool as it appears in a listing: compact, with '?' marking optional."""
    description = (tool_def.get("description") or "").strip()
    return {
        "server": server["label"],
        "tool": tool_def["name"],
        # Enough to choose a tool; the detail view carries the rest.
        "description": description[:LIST_DESCRIPTION_CHARS],
        "needs_confirmation": tool_mode(server, tool_def) == "confirm",
        "args": _arg_summary(tool_def),
    }


def _list_tools(
    user_id: str, is_background: bool, is_pr_review: bool, mode: Optional[str],
    server: Optional[str] = None, tool: Optional[str] = None,
    query: Optional[str] = None, limit: int = DEFAULT_LIST_LIMIT,
) -> str:
    """Discovery, narrowing in three steps so no step returns an unbounded blob.

    Bare: labels and counts only -- deliberately NOT tool names. Ten servers
    with 150 tools each would otherwise return 1500 names and recreate the
    context blowup this indirection exists to prevent.

    ``query`` without ``server`` searches across every server, so finding a
    tool among ten of them costs one call instead of ten.

    ``tool`` returns the full description and JSON Schema for that one tool.
    Listings carry a compact arg summary instead, because full schemas are 80%
    of a listing's bytes.
    """
    try:
        limit = max(1, min(int(limit), MAX_LIST_LIMIT))
    except (TypeError, ValueError):
        limit = DEFAULT_LIST_LIMIT

    pairs = _visible(user_id, is_background, is_pr_review, mode)
    if not pairs:
        return json.dumps({"servers": [], "message": "No MCP servers are registered."})

    if server:
        target = slugify_label(server)
        pairs = [(s, t) for s, t in pairs if s["label"] == target]
        if not pairs:
            known = sorted({s["label"] for s, _ in _visible(user_id, is_background, is_pr_review, mode)})
            return json.dumps({"error": f"Unknown MCP server '{server}'", "known_servers": known})

    # One named tool: the only place a full JSON Schema is returned, since it is
    # bounded to a single tool and is exactly what building a call needs.
    if tool:
        match = next((p for p in pairs if p[1]["name"] == tool), None)
        if match is None:
            return json.dumps({
                "error": f"No tool '{tool}'" + (f" on '{server}'" if server else ""),
                "hint": "Use mcp_list_tools(server=...) for exact names.",
            })
        srv, tool_def = match
        return json.dumps({
            "server": srv["label"],
            "tool": tool_def["name"],
            "description": (tool_def.get("description") or "").strip(),
            "needs_confirmation": tool_mode(srv, tool_def) == "confirm",
            "inputSchema": tool_def.get("inputSchema") or {},
        })

    if query:
        pairs = [(s, t) for s, t in pairs if _matches(t, query)]

    # Neither filter given: the server index, with counts but no tool names.
    if not server and not query:
        counts: Dict[str, int] = {}
        for s, _ in pairs:
            counts[s["label"]] = counts.get(s["label"], 0) + 1
        return json.dumps({
            "servers": [{"server": label, "tool_count": n} for label, n in sorted(counts.items())],
            "next_step": (
                "Call mcp_list_tools(server=...) to see one server's tools, or "
                "mcp_list_tools(query=...) to search across all of them."
            ),
        })

    total = len(pairs)
    return json.dumps({
        "tools": [_shape(s, t) for s, t in pairs[:limit]],
        "returned": min(total, limit),
        "total_matches": total,
        "args_legend": "name:type, '?' means optional. Use tool= for the full schema.",
        **({"hint": f"{total} matches; showing {limit}. Narrow with query= or raise limit."}
           if total > limit else {}),
    })


def get_custom_mcp_tools(
    user_id: str,
    is_background: bool = False,
    is_pr_review: bool = False,
    mode: Optional[str] = None,
    tool_capture: Any = None,
    send_tool_start: Optional[Callable] = None,
    send_tool_completion: Optional[Callable] = None,
    send_tool_error: Optional[Callable] = None,
    run_async_in_thread: Optional[Callable] = None,
    wrap_func_with_capture: Optional[Callable] = None,
) -> List[StructuredTool]:
    """Two tools that reach every registered MCP tool, however many there are.

    Registering one StructuredTool per remote tool put every name, description
    and JSON schema into the prompt on every turn: three servers with 150 tools
    each cost ~160k tokens before the user said anything. These two cost a fixed
    ~400 and read the same Vault cache on demand.

    The price is that the model can now name any string, so every rule that used
    to be enforced by *not building* a tool is enforced by refusing the call --
    see ``_call_tool``.
    """
    if not list_servers(user_id):
        return []

    def _list(server: Optional[str] = None, tool: Optional[str] = None,
              query: Optional[str] = None, limit: int = DEFAULT_LIST_LIMIT, **_: Any) -> str:
        return _list_tools(
            user_id, is_background, is_pr_review, mode,
            server=server, tool=tool, query=query, limit=limit,
        )

    def _call(server: str, tool: str, arguments: Optional[Dict[str, Any]] = None, **_: Any) -> str:
        return _call_tool(
            user_id, server, tool, arguments,
            is_background=is_background, is_pr_review=is_pr_review, mode=mode,
            send_tool_start=send_tool_start,
            send_tool_completion=send_tool_completion,
            send_tool_error=send_tool_error,
            run_async_in_thread=run_async_in_thread,
        )

    if tool_capture and wrap_func_with_capture:
        _list = wrap_func_with_capture(_list, "mcp_list_tools")
        _call = wrap_func_with_capture(_call, "mcp_call_tool")

    labels = ", ".join(s["label"] for s in list_servers(user_id))
    # metadata keeps both tools past ModeAccessController's blanket `mcp_` block;
    # Ask mode is then enforced per tool inside _visible/_call_tool.
    return [
        StructuredTool.from_function(
            func=_list,
            name="mcp_list_tools",
            description=(
                f"Discover tools on your organisation's MCP servers ({labels}). "
                "No arguments gives the server list and tool counts; server= lists "
                "one server's tools; query= searches across all servers; "
                "server= with tool= gives one tool's full input schema. "
                "Then use mcp_call_tool."
            ),
            args_schema=McpListToolsArgs,
            metadata={"mcp_read_only": True},
        ),
        StructuredTool.from_function(
            func=_call,
            name="mcp_call_tool",
            description=(
                "Invoke a tool found via mcp_list_tools. Pass the exact server and "
                "tool names it returned, plus arguments matching that tool's "
                "inputSchema. Write tools ask the user for confirmation."
            ),
            args_schema=McpCallToolArgs,
            metadata={"mcp_read_only": True},
        ),
    ]

