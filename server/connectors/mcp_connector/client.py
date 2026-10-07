"""MCP protocol client for customer-registered remote MCP servers.

Remote transports only: Streamable HTTP, or HTTP+SSE for servers that predate
it. The transport is chosen per server at registration, not guessed. stdio is
deliberately unsupported -- spawning a customer-supplied command inside the
Aurora container would be remote code execution.

No connection pooling or session reuse -- each call opens a session and closes
it. See the ponytail note on ``_run``.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple, TypeVar

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client

from connectors.mcp_connector.net import allow_private_targets, assert_allowed_target
from connectors.mcp_connector.oauth import OAuthDiscoveryError, is_expired, refresh_token

__all__ = [
    "AUTH_TYPES",
    "MAX_DESCRIPTION_CHARS",
    "MAX_TOOLS_PER_SERVER",
    "TRANSPORTS",
    "MCPAuthError",
    "MCPConnectionError",
    "allow_private_targets",
    "assert_allowed_target",
    "build_headers",
    "call",
    "flatten_content",
    "probe",
]

_T = TypeVar("_T")

HANDSHAKE_TIMEOUT = 20.0
CALL_TIMEOUT = 60.0

# Every registered tool's name, description, and JSON schema enters the system
# prompt on every turn, so this is a prompt-budget limit, not politeness. Set
# above the largest server observed (GitHub's MCP exposes 49) while still
# stopping one server from crowding out Aurora's own tools.
MAX_TOOLS_PER_SERVER = 64
MAX_DESCRIPTION_CHARS = 1000

# tools/list is paginated and the SDK does not follow the cursor for us. Bound
# the walk: a buggy or adversarial server returning a constant nextCursor would
# otherwise spin until the request times out.
MAX_TOOL_PAGES = 10

TRANSPORTS = ("streamable_http", "sse")
AUTH_TYPES = ("bearer", "header", "oauth", "none")


class MCPConnectionError(Exception):
    """Transport, handshake, or protocol failure against a remote MCP server."""


class MCPAuthError(MCPConnectionError):
    """Server rejected our credentials (HTTP 401/403).

    Distinct from its parent so the routes can answer "check the token" instead
    of "cannot reach the server", which sends the user chasing the wrong thing.
    """


def _is_auth_failure(exc: BaseException) -> bool:
    """True when the exception chain carries a genuine HTTP 401/403.

    Status code only -- never message text. A proxy's 403 body, a gateway page,
    or a tool result that merely mentions "401" must not be reported as "check
    your token", which sends the user to fix a credential that was never the
    problem. This also gates the OAuth refresh retry, so a false positive would
    hammer the vendor's token endpoint on an unrelated network error.
    """
    response = getattr(exc, "response", None)
    if getattr(response, "status_code", None) in (401, 403):
        return True
    if getattr(exc, "status_code", None) in (401, 403):
        return True
    for sub in getattr(exc, "exceptions", ()) or ():
        if _is_auth_failure(sub):
            return True
    cause = exc.__cause__
    return cause is not None and _is_auth_failure(cause)


def build_headers(auth: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Build request headers from a stored auth config. Never logged."""
    if not auth:
        return {}
    auth_type = (auth.get("type") or "none").lower()
    if auth_type in ("bearer", "oauth"):
        token = auth.get("token") or auth.get("access_token") or ""
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
    on_refresh: "Optional[Callable[[Dict[str, Any]], None]]" = None,
) -> Tuple[_T, str]:
    """Open a session on the configured transport and run ``op``.

    For OAuth servers the token is refreshed proactively when already expired,
    and reactively on a 401 (one retry). ``on_refresh`` is called with the new
    auth blob so the caller can persist it -- most servers rotate the refresh
    token, and dropping the new one disconnects the server at the next expiry.

    No automatic transport fallback: the user picks the transport when
    registering, and silently trying the other one turned a single wrong URL
    into two confusing failures. SSE stays selectable for servers that only
    speak the older HTTP+SSE protocol.

    ponytail: one handshake per operation, no session reuse or pooling. Ceiling
    is added latency on turns that make several calls to the same server.
    Upgrade path: cache the session per (user, server) behind a short TTL.
    """
    assert_allowed_target(url)
    chosen = transport if transport in TRANSPORTS else "streamable_http"

    # Refresh up front when the token is already known to be expired: the
    # request would fail anyway, and this saves a guaranteed-404 round-trip to
    # the customer's server on every call after expiry.
    if is_expired(auth) and _can_refresh(auth):
        auth = await _refresh(auth)
        if on_refresh:
            on_refresh(auth)

    try:
        return await _attempt(url, auth, chosen, op), chosen
    except MCPAuthError:
        # Retried exactly once, and only when a refresh could plausibly help. A
        # second failure means the grant was revoked; looping would hammer the
        # vendor's token endpoint.
        if not _can_refresh(auth):
            raise
        refreshed = await _refresh(auth)
        if on_refresh:
            on_refresh(refreshed)
        return await _attempt(url, refreshed, chosen, op), chosen


def _can_refresh(auth: Optional[Dict[str, Any]]) -> bool:
    return bool(
        auth
        and auth.get("type") == "oauth"
        and auth.get("refresh_token")
        and auth.get("token_endpoint")
    )


async def _refresh(auth: Dict[str, Any]) -> Dict[str, Any]:
    """Refresh an expired OAuth token, mapping failure to a reconnect prompt."""
    try:
        return await refresh_token(auth)
    except OAuthDiscoveryError as exc:
        raise MCPAuthError(
            f"This server's authorization has expired and could not be renewed "
            f"({exc}). Reconnect it."
        ) from exc


async def _attempt(
    url: str,
    auth: Optional[Dict[str, Any]],
    chosen: str,
    op: "Callable[[ClientSession], Awaitable[_T]]",
) -> _T:
    """One handshake-and-run against the chosen transport."""
    headers = build_headers(auth)
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
                return await op(session)
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
    cleaned = {
        "name": name,
        "description": description,
        "inputSchema": schema if isinstance(schema, dict) else {},
    }
    # Keep only the two hints that drive classification. The SDK model carries
    # more (title, idempotentHint, openWorldHint) that we would be storing in
    # Vault for nothing.
    annotations = getattr(tool, "annotations", None)
    hints = {
        key: getattr(annotations, key)
        for key in ("readOnlyHint", "destructiveHint")
        if isinstance(getattr(annotations, key, None), bool)
    } if annotations is not None else {}
    if hints:
        cleaned["annotations"] = hints
    return cleaned


async def probe(
    url: str,
    auth: Optional[Dict[str, Any]] = None,
    transport: str = "streamable_http",
    on_refresh: "Optional[Callable[[Dict[str, Any]], None]]" = None,
) -> Tuple[List[Dict[str, Any]], str]:
    """Handshake and list every tool. Returns (tools, transport_used).

    Raises ``ValueError`` for a rejected URL, ``MCPAuthError`` for bad
    credentials, ``MCPConnectionError`` for anything else.
    """

    async def _list(session: ClientSession) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        seen: set = set()
        cursor: Optional[str] = None
        for _ in range(MAX_TOOL_PAGES):
            page = await session.list_tools(cursor)
            for tool in page.tools or []:
                cleaned = _clean_tool(tool)
                # A server that repeats a cursor would otherwise duplicate tools.
                if cleaned and cleaned["name"] not in seen:
                    seen.add(cleaned["name"])
                    out.append(cleaned)
            cursor = getattr(page, "nextCursor", None)
            if not cursor:
                break
        return out

    tools, used = await _run(url, auth, transport, _list, on_refresh)
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
    on_refresh: "Optional[Callable[[Dict[str, Any]], None]]" = None,
) -> str:
    """Invoke one tool and return its content flattened to text."""

    async def _call(session: ClientSession) -> str:
        result = await session.call_tool(tool_name, arguments or {})
        return flatten_content(result)

    text, _used = await _run(url, auth, transport, _call, on_refresh)
    return text
