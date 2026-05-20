"""
Aurora MCP Server — bootstrap + middleware + identity proxy.

Tool/resource/prompt definitions live in the `server/mcp/` package; this
file just wires the FastMCP instance, owns the bearer-token middleware,
and provides the shared `_api(method, path, ...)` helper that forwards
user identity (resolved from the MCP token) to the Flask backend.

Runs as a streamable-http server on port 8811 (default).
"""

from __future__ import annotations

import asyncio
import atexit
import contextvars
import logging
import os
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import unquote

import httpx
import psycopg2
import psycopg2.pool
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger("aurora.mcp")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

API_BASE = os.environ.get("BACKEND_URL", "http://aurora-server:5080")
_AURORA_ENV = os.environ.get("AURORA_ENV", "production")
_INTERNAL_SECRET = os.environ.get("INTERNAL_API_SECRET", "")

if not _INTERNAL_SECRET:
    if _AURORA_ENV == "dev":
        logger.warning("INTERNAL_API_SECRET not set (AURORA_ENV='dev') — MCP proxy auth disabled for local development")
    else:
        raise RuntimeError(
            "FATAL: INTERNAL_API_SECRET is not set and AURORA_ENV='%s'. "
            "Refusing to start MCP proxy without authentication secrets." % _AURORA_ENV
        )

_current_bearer_token: contextvars.ContextVar[str] = contextvars.ContextVar("_current_bearer_token")


class BearerTokenMiddleware:
    """ASGI middleware that extracts Bearer token and stores it in a ContextVar."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            request = Request(scope)
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                token = _current_bearer_token.set(auth[7:])
                try:
                    await self.app(scope, receive, send)
                finally:
                    _current_bearer_token.reset(token)
                return
        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# Token resolution (only direct DB access in this server)
# ---------------------------------------------------------------------------

_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None
_last_used_cache: Dict[str, float] = {}

# token → (resolved_at_monotonic, user_id, org_id). Avoids a Postgres SELECT
# on every list_tools / call_tool request.
_TOKEN_READ_TTL = 60.0
_token_read_cache: Dict[str, tuple] = {}

# Hard ceiling on both caches. The TTL only controls re-reads, not eviction;
# without a bound, rotated/expired tokens would accumulate forever. We never
# expect more than a few hundred distinct active MCP tokens per process.
_TOKEN_CACHE_MAX = 1024


def _prune_token_caches() -> None:
    """Evict the oldest entries once either cache exceeds _TOKEN_CACHE_MAX.

    Both caches are keyed by token; we drop the bottom half (by recency) so
    pruning is amortized O(N/2) per insert rather than O(1) per insert.
    """
    if len(_token_read_cache) > _TOKEN_CACHE_MAX:
        # _token_read_cache value tuple = (resolved_at_monotonic, user_id, org_id)
        cutoff = sorted(_token_read_cache.items(), key=lambda kv: kv[1][0])
        for tok, _ in cutoff[: len(cutoff) // 2]:
            _token_read_cache.pop(tok, None)
    if len(_last_used_cache) > _TOKEN_CACHE_MAX:
        cutoff = sorted(_last_used_cache.items(), key=lambda kv: kv[1])
        for tok, _ in cutoff[: len(cutoff) // 2]:
            _last_used_cache.pop(tok, None)


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None or _pool.closed:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=10,
            host=os.environ["POSTGRES_HOST"],
            port=os.environ.get("POSTGRES_PORT", "5432"),
            dbname=os.environ["POSTGRES_DB"],
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
            sslmode=os.environ.get("POSTGRES_SSLMODE", "prefer") or None,
            sslrootcert=os.environ.get("POSTGRES_SSLROOTCERT") or None,
        )
    return _pool


def _shutdown_pool() -> None:
    if _pool is not None and not _pool.closed:
        _pool.closeall()


atexit.register(_shutdown_pool)


def _resolve_token(token: str) -> Tuple[str, str]:
    """Look up an MCP API token and return (user_id, org_id).

    Result is cached for _TOKEN_READ_TTL seconds. Revocations therefore take
    up to that long to be observed by the MCP process — acceptable for a
    bearer-token gating path used several times per MCP request.
    """
    now = time.monotonic()
    cached = _token_read_cache.get(token)
    if cached is not None and now - cached[0] < _TOKEN_READ_TTL:
        return cached[1], cached[2]

    pool = _get_pool()
    conn = pool.getconn()
    ok = False
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL myapp.mcp_token_resolve = 'true'")
            cur.execute(
                "SELECT user_id, org_id FROM mcp_tokens "
                "WHERE token = %s AND status = 'active' "
                "AND (expires_at IS NULL OR expires_at > NOW())",
                (token,),
            )

            row = cur.fetchone()
            if not row:
                raise ValueError("Invalid, expired, or revoked MCP token")
            if now - _last_used_cache.get(token, 0) > 60:
                cur.execute("UPDATE mcp_tokens SET last_used_at = NOW() WHERE token = %s", (token,))
                _last_used_cache[token] = now
        conn.commit()
        ok = True
        _token_read_cache[token] = (now, row[0], row[1])
        _prune_token_caches()
        return row[0], row[1]
    finally:
        if not ok:
            conn.rollback()
        pool.putconn(conn, close=not ok)


def _get_token() -> str:
    """Extract token from the Bearer header (via ContextVar set by middleware)."""
    try:
        return _current_bearer_token.get()
    except LookupError:
        raise ValueError("No MCP token provided. Send a Bearer token in the Authorization header.")


_http_client: Optional[httpx.AsyncClient] = None


def _shutdown_http_client() -> None:
    """Close the shared httpx.AsyncClient on process exit.

    We schedule .aclose() on a fresh event loop because atexit handlers run
    after the main loop is gone. Best-effort — swallow failures so shutdown
    doesn't block on a half-torn-down runtime.
    """
    client = _http_client
    if client is None or client.is_closed:
        return
    try:
        asyncio.run(client.aclose())
    except Exception:
        logger.debug("error closing shared httpx client at exit", exc_info=True)


atexit.register(_shutdown_http_client)


def _get_http_client() -> httpx.AsyncClient:
    """Lazily build a long-lived httpx client with keepalive pooling.

    Reusing the client across requests preserves the TCP/TLS connection to
    aurora-server, which matters in tight poll loops like chat_with_aurora.
    """
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            base_url=API_BASE,
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
            # Outer safety net per request. Tighter per-call deadlines are enforced
            # via `async with asyncio.timeout(N)` at call sites (15s polls, 60s RCA).
            timeout=httpx.Timeout(60.0),
        )
    return _http_client


async def _api(
    method: str,
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Proxy a request to the Aurora Flask API with identity from the MCP token.

    Default request timeout is 30s (configured on the shared httpx client).
    Callers that need a different deadline wrap with `async with asyncio.timeout(N)`.
    """
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError(f"Path must be a relative path starting with /: {path}")
    # Defense-in-depth: httpx.URL collapses `..` segments, so an unencoded
    # path traversal would escape the intended prefix even though the path
    # "starts with /". Callers must URL-encode user input (see
    # urllib.parse.quote in tools_gated/dispatch); reject anything whose
    # *decoded* form still resolves to a parent segment so mixed encodings
    # like `/.%2e/admin` or `/%2e./admin` can't slip through a single
    # missed call site.
    decoded = unquote(path)
    if any(seg == ".." for seg in decoded.split("/")):
        raise ValueError("Path contains parent-directory segments")
    token = _get_token()
    user_id, org_id = _resolve_token(token)
    headers = {"X-User-ID": user_id, "X-Org-ID": org_id}
    if _INTERNAL_SECRET:
        headers["X-Internal-Secret"] = _INTERNAL_SECRET
    client = _get_http_client()
    resp = await client.request(
        method, path, params=params, json=body, headers=headers,
    )
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        try:
            detail: Any = exc.response.json()
        except (ValueError, httpx.ResponseNotRead):
            detail = exc.response.text[:500]
        # Full upstream body stays in the server log; the proxy error
        # surfaced to the MCP client carries only the status code so
        # accidental leakage from any upstream route can't escape here.
        logger.warning(
            "Aurora API %s %s returned %s: %s",
            method, path, code, detail,
        )
        raise ValueError(f"Aurora API returned status {code}") from exc
    return resp.json()


# ---------------------------------------------------------------------------
# FastMCP instance
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "Aurora",
    instructions=(
        "Aurora is an AI-powered cloud operations platform. For investigation, "
        "incident triage, or any open-ended question, prefer `chat_with_aurora` "
        "— Aurora's agent picks the right tools, runs RCAs, and cites sources. "
        "Use the connector-specific tools (query_logs, query_metrics, github_rca…) "
        "only when the user explicitly asks for raw data. Discover additional "
        "tools via search_tools and invoke them via call_tool."
    ),
    host="0.0.0.0",  # Bind all interfaces; auth is enforced via Bearer token
    stateless_http=True,
    json_response=True,
)


# ---------------------------------------------------------------------------
# Wire tools / resources / prompts from the server/mcp/ package
# ---------------------------------------------------------------------------

from aurora_mcp.tools_always_on import register_tier1_tools  # noqa: E402
from aurora_mcp.tools_gated import register_tier2_tools  # noqa: E402
from aurora_mcp.dispatch import register_dispatch_tools  # noqa: E402
from aurora_mcp.resources import register_resources  # noqa: E402
from aurora_mcp.prompts import register_prompts  # noqa: E402

register_tier1_tools(mcp, _api)
register_tier2_tools(mcp, _api, _get_token, _resolve_token)
register_dispatch_tools(mcp, _api, _get_token, _resolve_token)
register_resources(mcp, _api, _get_token, _resolve_token)
register_prompts(mcp)


# ---------------------------------------------------------------------------
# Per-request tool-list filtering — hides Tier-2 tools the user can't use.
#
# In stateless_http=True FastMCP can't push notifications/tools/list_changed,
# but we can still re-register the lowlevel ListToolsRequest handler to filter
# on every request. The bearer token is set by BearerTokenMiddleware before
# list_tools fires.
#
# Reassigning `mcp.list_tools` is NOT enough: FastMCP's _setup_handlers() runs
# in __init__ and captures the bound method at registration time via
# `self._mcp_server.list_tools()(self.list_tools)`. The dispatcher invokes
# the captured method, not whatever later replaces the attribute. We re-call
# that decorator here so request_handlers[ListToolsRequest] points at the
# filtered version.
# ---------------------------------------------------------------------------

from aurora_mcp.registry import (  # noqa: E402
    TIER2_TOOLS,
    _get_cached_connector_status,
    gated_tool_visible,
    parse_and_cache_connector_status,
)

_GATED_NAMES = {spec.name: spec for spec in TIER2_TOOLS}
_original_list_tools = mcp.list_tools  # bound method captured before re-register


async def _refresh_connector_cache(user_id: str) -> None:
    """Fetch connector status from the Flask backend and cache it.

    The backend's /api/connectors/status handles RLS correctly (Flask context),
    so this is authoritative. Maps provider names to skill IDs (they're the
    same in practice — e.g. "github" -> "github", "gcp" -> "gcp").
    """
    try:
        data = await _api("GET", "/api/connectors/status")
        parse_and_cache_connector_status(user_id, data)
    except Exception:
        logger.exception("connector cache refresh failed for user=%s", user_id)


@mcp._mcp_server.list_tools()
async def _filtered_list_tools():  # type: ignore[no-redef]
    all_tools = await _original_list_tools()
    # If the token isn't set yet (e.g. a probe before identifier injection),
    # default to showing only always-on tools to keep the upfront surface lean.
    try:
        token = _current_bearer_token.get()
        user_id, _org_id = _resolve_token(token)
    except Exception:
        logger.exception(
            "failed to resolve bearer token in _filtered_list_tools — "
            "falling back to always-on tools only"
        )
        return [t for t in all_tools if t.name not in _GATED_NAMES]

    # Populate connector cache from the backend API so that gated_tool_visible
    # (and downstream search_tools/call_tool) use the authoritative status.
    if _get_cached_connector_status(user_id) is None:
        await _refresh_connector_cache(user_id)

    filtered = []
    for t in all_tools:
        spec = _GATED_NAMES.get(t.name)
        if spec is None:
            filtered.append(t)
            continue
        if gated_tool_visible(spec, user_id):
            filtered.append(t)
    return filtered


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("MCP_PORT", "8811"))
    mcp.settings.port = port

    _original_app_factory = mcp.streamable_http_app

    def _patched_app_factory():
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        app = _original_app_factory()
        app.add_middleware(BearerTokenMiddleware)

        def _healthz(request):
            pool = _get_pool()
            try:
                conn = pool.getconn()
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT 1")
                finally:
                    pool.putconn(conn)
                return JSONResponse({"status": "ok"})
            except Exception:
                # Don't leak postgres error text to unauthenticated callers.
                logger.exception("healthz: db check failed")
                return JSONResponse({"status": "error"}, status_code=503)

        app.routes.append(Route("/healthz", _healthz, methods=["GET"]))
        return app

    mcp.streamable_http_app = _patched_app_factory
    logger.info(f"Starting Aurora MCP server on port {port}")
    mcp.run(transport="streamable-http")
