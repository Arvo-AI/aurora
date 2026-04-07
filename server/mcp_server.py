"""
Aurora MCP Server

Exposes Aurora's full API surface to MCP-compatible clients (Claude Desktop,
Cursor, Windsurf, etc.) via 5 curated tools for the core investigation workflow
and a generic proxy tool that covers all ~340 API endpoints.

Usage:
  python mcp_server.py                               # stdio (default)
  python mcp_server.py --transport streamable-http    # HTTP on port 8811
"""

import argparse
import asyncio
import contextvars
import logging
import os

import httpx
import psycopg2
import psycopg2.pool
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger("aurora.mcp")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

API_BASE = os.environ.get("MCP_API_BASE", "http://aurora-server:5080")

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

_pool: psycopg2.pool.ThreadedConnectionPool | None = None


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
        )
    return _pool


def _resolve_token(token: str) -> tuple[str, str]:
    """Look up an MCP API token and return (user_id, org_id)."""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, org_id FROM mcp_tokens "
                "WHERE token = %s AND status = 'active' "
                "AND (expires_at IS NULL OR expires_at > NOW())",
                (token,),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError("Invalid, expired, or revoked MCP token")
            cur.execute("UPDATE mcp_tokens SET last_used_at = NOW() WHERE token = %s", (token,))
            conn.commit()
            return row[0], row[1]
    finally:
        pool.putconn(conn)


def _get_token() -> str:
    """Extract token from context var (HTTP) or AURORA_MCP_TOKEN env var (stdio)."""
    try:
        return _current_bearer_token.get()
    except LookupError:
        pass
    token = os.environ.get("AURORA_MCP_TOKEN", "")
    if not token:
        raise ValueError("No MCP token provided. Set AURORA_MCP_TOKEN or send Bearer header.")
    return token


async def _api(method: str, path: str, *, params: dict | None = None,
               body: dict | None = None, timeout: float = 30) -> dict:
    """Proxy a request to the Aurora Flask API with identity from the MCP token."""
    token = _get_token()
    user_id, org_id = _resolve_token(token)
    headers = {"X-User-ID": user_id, "X-Org-ID": org_id}
    async with httpx.AsyncClient(base_url=API_BASE, timeout=timeout) as client:
        resp = await client.request(method, path, params=params, json=body, headers=headers)
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# FastMCP instance
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "Aurora",
    instructions=(
        "Aurora is an AI-powered cloud operations platform. "
        "Use the curated tools for incidents and infrastructure. "
        "For anything else, use aurora_api -- read aurora://api-catalog first to discover endpoints."
    ),
    stateless_http=True,
    json_response=True,
)


# ---------------------------------------------------------------------------
# Curated tools -- typed parameters, great UX for the core workflow
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_incidents(status: str | None = None, limit: int = 20) -> dict:
    """List Aurora incidents. Optionally filter by status (active/investigating/resolved)."""
    params: dict = {"limit": limit}
    if status:
        params["status"] = status
    return await _api("GET", "/api/incidents", params=params)


@mcp.tool()
async def get_incident(incident_id: str) -> dict:
    """Get full incident details including summary, suggestions, citations, and alerts."""
    return await _api("GET", f"/api/incidents/{incident_id}")


@mcp.tool()
async def ask_incident(incident_id: str, question: str) -> dict:
    """Ask Aurora AI a question about an incident. Posts the question and polls for the response."""
    result = await _api("POST", f"/api/incidents/{incident_id}/chat",
                        body={"message": question}, timeout=30)
    session_id = result.get("session_id")
    if not session_id:
        return result

    for _ in range(30):
        await asyncio.sleep(2)
        session = await _api("GET", f"/chat_api/sessions/{session_id}", timeout=15)
        if session.get("status") not in ("processing", "pending"):
            return session
    return {"status": "still_processing", "session_id": session_id,
            "message": "Response not ready yet. Poll aurora_api GET /chat_api/sessions/{session_id}"}


@mcp.tool()
async def get_graph_stats() -> dict:
    """Get infrastructure graph statistics: single points of failure, critical services, topology overview."""
    return await _api("GET", "/api/graph/stats")


@mcp.tool()
async def search_knowledge_base(query: str, limit: int = 5) -> dict:
    """Semantic search across Aurora's knowledge base documents."""
    return await _api("POST", "/api/knowledge-base/search", body={"query": query, "limit": limit})


# ---------------------------------------------------------------------------
# Generic proxy tool -- covers 100% of Aurora's API surface
# ---------------------------------------------------------------------------

@mcp.tool()
async def aurora_api(method: str, path: str, params: dict | None = None,
                     body: dict | None = None) -> dict:
    """Call any Aurora API endpoint. Read the aurora://api-catalog resource first to discover
    available endpoints. method: GET/POST/PATCH/PUT/DELETE. path: e.g. /api/connectors/status"""
    timeout = 120.0 if "discover" in path else 30.0
    return await _api(method, path, params=params, body=body, timeout=timeout)


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------

@mcp.resource("aurora://api-catalog")
async def api_catalog() -> str:
    """List of all available Aurora API endpoints (auto-generated from Flask route map)."""
    data = await _api("GET", "/api/routes")
    lines = []
    for route in data:
        methods = ", ".join(route["methods"])
        lines.append(f"{methods:30s} {route['path']}")
    return "\n".join(lines)


@mcp.resource("aurora://health")
async def health() -> dict:
    """Aurora system health: database, Redis, Weaviate, Celery status."""
    return await _api("GET", "/health/")


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

@mcp.prompt()
def investigate_incident(incident_id: str) -> str:
    """Structured prompt for investigating an Aurora incident."""
    return (
        f"Investigate Aurora incident #{incident_id}. Steps:\n"
        "1. get_incident to retrieve full details\n"
        "2. Review the AI-generated summary, suggestions, and citations\n"
        "3. Use ask_incident for follow-up questions\n"
        "4. Check get_graph_stats for infrastructure impact\n"
        "5. Search knowledge base for related runbooks\n"
        "6. Summarize findings with root cause, impact, and recommended actions"
    )


@mcp.prompt()
def blast_radius_analysis(service_name: str) -> str:
    """Analyze the blast radius of a failing service."""
    return (
        f"Analyze the blast radius for service '{service_name}':\n"
        "1. aurora_api GET /api/graph/services/{name}/impact to get downstream dependencies\n"
        "2. get_graph_stats for overall topology context\n"
        "3. list_incidents to check for active incidents on affected services\n"
        "4. Summarize: which services are at risk, estimated user impact, mitigation steps"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Aurora MCP Server")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--port", type=int, default=8811)
    args = parser.parse_args()

    if args.transport == "streamable-http":
        mcp.settings.host = "0.0.0.0"
        mcp.settings.port = args.port

        _original_app_factory = mcp.streamable_http_app

        def _patched_app_factory():
            app = _original_app_factory()
            app.add_middleware(BearerTokenMiddleware)
            return app

        mcp.streamable_http_app = _patched_app_factory
        logger.info(f"Starting Aurora MCP server on port {args.port} (streamable-http)")
    else:
        logger.info("Starting Aurora MCP server (stdio)")

    mcp.run(transport=args.transport)
