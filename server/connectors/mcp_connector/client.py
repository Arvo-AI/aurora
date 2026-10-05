"""MCP protocol client for customer-registered remote MCP servers.

Remote transports only: Streamable HTTP, or HTTP+SSE for servers that predate
it. The transport is chosen per server at registration, not guessed. stdio is
deliberately unsupported -- spawning a customer-supplied command inside the
Aurora container would be remote code execution.

No connection pooling or session reuse -- each call opens a session and closes
it. See the ponytail note on ``_run``.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple, TypeVar
from urllib.parse import urlparse

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client

_T = TypeVar("_T")

HANDSHAKE_TIMEOUT = 20.0
CALL_TIMEOUT = 60.0

MAX_TOOLS_PER_SERVER = 25
MAX_DESCRIPTION_CHARS = 1000

TRANSPORTS = ("streamable_http", "sse")
AUTH_TYPES = ("bearer", "header", "none")


class MCPConnectionError(Exception):
    """Transport, handshake, or protocol failure against a remote MCP server."""


class MCPAuthError(MCPConnectionError):
    """Server rejected our credentials (HTTP 401/403).

    Distinct from its parent so the routes can answer "check the token" instead
    of "cannot reach the server", which sends the user chasing the wrong thing.
    """


def _is_auth_failure(exc: BaseException) -> bool:
    """True if the exception chain carries an HTTP 401/403."""
    for err in (exc, *getattr(exc, "exceptions", ())):
        status = getattr(getattr(err, "response", None), "status_code", None) or getattr(
            err, "status_code", None
        )
        if status in (401, 403):
            return True
        text = str(err)
        if "401" in text or "403" in text or "Unauthorized" in text:
            return True
        if err.__cause__ is not None and _is_auth_failure(err.__cause__):
            return True
    return False


def allow_private_targets() -> bool:
    """Whether private/internal addresses are valid MCP targets.

    False on Aurora SaaS: a tenant must not be able to aim Aurora at Arvo's
    internal network. True for self-hosted installs, where the customer's MCP
    servers legitimately live on private addresses inside their own cluster.
    """
    return os.getenv("MCP_ALLOW_PRIVATE_TARGETS", "false").strip().lower() == "true"


def assert_allowed_target(url: str) -> None:
    """Validate an MCP server URL, rejecting non-public hosts unless allowed.

    Raises ``ValueError`` with a user-facing message. Mirrors the SSRF guard in
    ``chat/backend/agent/tools/notion/workspace.py``: every resolved address
    must pass, so a hostname with both a public and a loopback A record cannot
    slip through.

    ponytail: resolve-then-connect leaves a TOCTOU window (DNS can change
    between this check and the request). Accepted, same as the Notion path.
    Closing it needs a custom resolver pinning the validated IP.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("URL must start with http:// or https://")
    host = parsed.hostname
    if not host:
        raise ValueError("URL has no hostname")

    if allow_private_targets():
        return

    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise ValueError(f"DNS lookup failed for {host}: {exc}") from exc

    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise ValueError(
                f"{host} resolves to the non-public address {ip}. Aurora refuses "
                "internal targets; set MCP_ALLOW_PRIVATE_TARGETS=true on a "
                "self-hosted deployment to allow them."
            )


def build_headers(auth: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Build request headers from a stored auth config. Never logged."""
    if not auth:
        return {}
    auth_type = (auth.get("type") or "none").lower()
    if auth_type == "bearer":
        token = auth.get("token") or ""
        return {"Authorization": f"Bearer {token}"} if token else {}
    if auth_type == "header":
        name, value = auth.get("header_name"), auth.get("token")
        return {name: value} if name and value else {}
    return {}


async def _run(
    url: str,
    auth: Optional[Dict[str, Any]],
    transport: str,
    op: "Callable[[ClientSession], Awaitable[_T]]",
) -> Tuple[_T, str]:
    """Open a session on the configured transport and run ``op``.

    No automatic transport fallback: the user picks the transport when
    registering, and silently trying the other one turned a single wrong URL
    into two confusing failures. SSE stays selectable for servers that only
    speak the older HTTP+SSE protocol.

    ponytail: one handshake per operation, no session reuse or pooling. Ceiling
    is added latency on turns that make several calls to the same server.
    Upgrade path: cache the session per (user, server) behind a short TTL.
    """
    assert_allowed_target(url)
    headers = build_headers(auth)
    chosen = transport if transport in TRANSPORTS else "streamable_http"
    client = (
        sse_client(url, headers=headers, timeout=HANDSHAKE_TIMEOUT)
        if chosen == "sse"
        else streamablehttp_client(url, headers=headers, timeout=HANDSHAKE_TIMEOUT)
    )
    try:
        async with client as streams:
            # streamablehttp_client yields a third element (a session-id getter).
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                return await op(session), chosen
    except Exception as exc:
        if _is_auth_failure(exc):
            raise MCPAuthError(
                "Server rejected the credentials (HTTP 401/403). Check the token."
            ) from exc
        raise MCPConnectionError(_describe(exc)) from exc


def _describe(exc: BaseException) -> str:
    """Readable one-line cause for an exception, unwrapping ExceptionGroups.

    The MCP SDK runs its transport inside an anyio task group, so a plain
    connection refusal arrives as "unhandled errors in a TaskGroup (1
    sub-exception)" -- useless to a user who mistyped a URL. Walk into the
    group and the ``__cause__`` chain for the message that actually explains it.
    """
    for sub in getattr(exc, "exceptions", ()) or ():
        described = _describe(sub)
        if described:
            return described
    text = str(exc).strip()
    if not text or "unhandled errors in a TaskGroup" in text:
        if exc.__cause__ is not None:
            return _describe(exc.__cause__)
        return type(exc).__name__
    return f"{type(exc).__name__}: {text}" if len(text) < 40 else text


def _clean_tool(tool: Any) -> Optional[Dict[str, Any]]:
    """Reduce an SDK Tool to the JSON we cache, or None if unusable."""
    name = getattr(tool, "name", "") or ""
    if not name:
        return None
    description = (getattr(tool, "description", "") or "")[:MAX_DESCRIPTION_CHARS]
    schema = getattr(tool, "inputSchema", None)
    return {
        "name": name,
        "description": description,
        "inputSchema": schema if isinstance(schema, dict) else {},
    }


async def probe(
    url: str, auth: Optional[Dict[str, Any]] = None, transport: str = "streamable_http"
) -> Tuple[List[Dict[str, Any]], str]:
    """Handshake and list tools. Returns (tools, transport_used).

    Raises ``ValueError`` for a rejected URL, ``MCPAuthError`` for bad
    credentials, ``MCPConnectionError`` for anything else.
    """

    async def _list(session: ClientSession) -> List[Dict[str, Any]]:
        result = await session.list_tools()
        return [t for t in (_clean_tool(t) for t in (result.tools or [])) if t]

    tools, used = await _run(url, auth, transport, _list)
    if not tools:
        raise MCPConnectionError("Server completed the handshake but exposed no tools")
    # Not truncated here: the caller filters by read/write first, so capping now
    # could hide a server's read tools behind its writes.
    return tools, used


def flatten_content(result: Any) -> str:
    """Flatten an MCP CallToolResult into text for the LLM."""
    parts: List[str] = []
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if text:
            parts.append(text)
            continue
        resource = getattr(item, "resource", None)
        res_text = getattr(resource, "text", None) if resource else None
        if res_text:
            parts.append(res_text)
        elif resource is not None and getattr(resource, "blob", None):
            parts.append("[binary content omitted]")
        else:
            parts.append(str(item))
    joined = "\n".join(p for p in parts if p)
    if getattr(result, "isError", False):
        return f"Tool reported an error: {joined or 'no detail provided'}"
    return joined or "(tool returned no content)"


async def call(
    url: str,
    auth: Optional[Dict[str, Any]],
    transport: str,
    tool_name: str,
    arguments: Dict[str, Any],
) -> str:
    """Invoke one tool and return its content flattened to text."""

    async def _call(session: ClientSession) -> str:
        result = await session.call_tool(tool_name, arguments or {})
        return flatten_content(result)

    text, _used = await _run(url, auth, transport, _call)
    return text
