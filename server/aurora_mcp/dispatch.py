"""Tier-3 dispatch — search_tools + call_tool for the long tail.

`search_tools(query, category?, connector?)` returns a small list of
matching entries (name, description, args). `call_tool(name, args)` invokes
the entry, but only if it's in DISPATCH_ALLOWLIST. This is the hard
security boundary for the long tail — infra-write surfaces are not in the
allowlist and therefore unreachable from MCP.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .registry import (
    dispatch_entry_visible,
    find_dispatch_entry,
    search_dispatch_entries,
)
from .response import truncate_payload

logger = logging.getLogger(__name__)

ApiCall = Callable[..., Awaitable[Dict[str, Any]]]

_PATH_ARG_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def register_dispatch_tools(
    mcp,
    api_call: ApiCall,
    get_token: Callable[[], str],
    resolve_token: Callable[[str], Any],
) -> None:
    """Register search_tools and call_tool on the FastMCP instance."""

    def _user_id() -> str:
        token = get_token()
        user_id, _ = resolve_token(token)
        return user_id

    @mcp.tool()
    async def search_tools(
        query: str = "",
        category: Optional[str] = None,
        connector: Optional[str] = None,
        limit: int = 10,
    ) -> Dict[str, Any]:
        """Discover tools beyond the always-visible set. Use this when the user
        asks for something a top-level tool doesn't cover.

        Args:
          query: free-text search over tool name and description (optional).
          category: filter to a category (e.g. "logs", "ticketing", "code").
          connector: filter to entries gated by a specific skill (e.g. "jira").
          limit: max results (default 10).

        Results that need a connector you haven't connected appear as "not
        connected" and won't be callable until you connect them in Aurora.
        """
        user_id = _user_id()
        # Single pass: pull all matches (no visibility filter), tag each with
        # callable_now in one walk. Visible entries appear first so the LLM
        # sees them; non-visible ones are kept for discoverability ("here's
        # what exists, connect to use it").
        all_matches = search_dispatch_entries(
            query=query, category=category, connector=connector,
            user_id=None, limit=limit * 2,
        )
        annotated = [(e, dispatch_entry_visible(e, user_id)) for e in all_matches]
        ordered = (
            [(e, True) for e, v in annotated if v]
            + [(e, False) for e, v in annotated if not v]
        )[:limit]

        def _shape(entry, callable_now: bool):
            return {
                "name": entry.name,
                "description": entry.description,
                "category": entry.category,
                "callable_now": callable_now,
                "enabling_skills": list(entry.enabling_skills),
                "args": _arg_schema(entry),
            }

        return truncate_payload(
            {
                "tools": [_shape(e, c) for e, c in ordered],
                "total_matches": len(ordered),
                "hint": (
                    "Call_tool requires a tool whose `callable_now` is true. "
                    "Connect the integration in Aurora to enable the rest."
                ),
            },
            tool_name="search_tools",
        )

    @mcp.tool()
    async def call_tool(name: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Invoke a long-tail tool returned by search_tools.

        Args:
          name: exact tool name from search_tools.
          args: keyword arguments. See the tool's `args` field in search_tools
                for the expected keys.

        Refuses calls to anything not in the MCP allowlist. Infra-write tools
        (Terraform apply, kubectl mutations, shell exec, Cloudflare WAF) are
        deliberately excluded.
        """
        entry = find_dispatch_entry(name)
        if entry is None:
            return {
                "error": "tool_not_allowlisted",
                "name": name,
                "hint": "Use search_tools to discover callable tool names.",
            }

        user_id = _user_id()
        if not dispatch_entry_visible(entry, user_id):
            return {
                "error": "not_connected",
                "name": name,
                "enabling_skills": list(entry.enabling_skills),
                "hint": "Connect at least one enabling integration in Aurora.",
            }

        args = dict(args or {})

        # Substitute path arguments.
        path = entry.path
        for path_arg in entry.path_args:
            value = args.pop(path_arg, None)
            if value is None:
                return {"error": "missing_path_arg", "arg": path_arg, "tool": name}
            path = path.replace("{" + path_arg + "}", str(value))

        # Any {placeholder} that wasn't declared in path_args is a bug.
        leftover = _PATH_ARG_RE.findall(path)
        if leftover:
            return {"error": "unresolved_path_args", "args": leftover, "tool": name}

        # Split remaining args into body vs query.
        body: Dict[str, Any] = {}
        params: Dict[str, Any] = {}
        for k, v in args.items():
            if k in entry.body_keys:
                body[k] = v
            else:
                params[k] = v

        kwargs: Dict[str, Any] = {}
        if entry.method.upper() in ("POST", "PUT", "PATCH") and body:
            kwargs["body"] = body
        if params:
            kwargs["params"] = params

        result = await api_call(entry.method.upper(), path, **kwargs)
        return truncate_payload(result, tool_name=name)


def _arg_schema(entry) -> List[Dict[str, Any]]:
    """Render a small per-arg schema for search_tools output."""
    out: List[Dict[str, Any]] = []
    for a in entry.path_args:
        out.append({"name": a, "in": "path", "required": True})
    for a in entry.body_keys:
        out.append({"name": a, "in": "body", "required": False})
    return out
