"""MCP resources — URI-fetched data, zero upfront token cost."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, List

from .response import truncate_payload

logger = logging.getLogger(__name__)

ApiCall = Callable[..., Awaitable[Dict[str, Any]]]

_INTERNAL_ERROR: Dict[str, Any] = {
    "error": "internal_server_error",
    "message": "An internal error occurred",
}


def _mcp_covered_skills() -> frozenset:
    """Connectors that have at least one MCP-callable tool (Tier 2 or 3).

    Used to flag connectors (e.g. AWS) that show as connected in /api/connectors/status
    but have no surface in any MCP tier — so the LLM knows to use `search_tools`
    to discover available capabilities instead of asking for a non-existent tool.
    """
    from .registry import DISPATCH_ALLOWLIST, TIER2_TOOLS
    skills = set()
    for entry in DISPATCH_ALLOWLIST:
        skills.update(s.lower() for s in entry.enabling_skills)
    for spec in TIER2_TOOLS:
        skills.update(s.lower() for s in spec.enabling_skills)
    return frozenset(skills)


def _annotate_connector_coverage(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Add mcp_tools_available to each connector entry in /api/connectors/status."""
    if not isinstance(payload, dict):
        return payload
    connectors = payload.get("connectors")
    if not isinstance(connectors, dict):
        return payload
    covered = _mcp_covered_skills()
    for cid, info in connectors.items():
        if isinstance(info, dict):
            info["mcp_tools_available"] = cid.lower() in covered
    return payload


async def _do_catalog_connectors(api_call: ApiCall) -> Dict[str, Any]:
    try:
        data = await api_call("GET", "/api/connectors/status")
        return truncate_payload(
            _annotate_connector_coverage(data),
            tool_name="catalog/connectors",
        )
    except Exception:
        logger.exception("catalog/connectors failed")
        return dict(_INTERNAL_ERROR)


def _do_whoami(user_id: str, org_id: str) -> Dict[str, Any]:
    """Identity the MCP bearer token resolves to. Useful for clients to
    sanity-check 'why can't I see X' issues without a backend round-trip."""
    return {"user_id": user_id, "org_id": org_id}


def _shape_skill_entry(skill_id: str, meta: Any, connected: bool) -> Dict[str, Any]:
    return {
        "id": skill_id,
        "name": getattr(meta, "name", skill_id),
        "category": getattr(meta, "category", "") if meta else "",
        "connected": bool(connected),
    }


def _do_catalog_skills(user_id: str) -> Dict[str, Any]:
    try:
        from chat.backend.agent.skills.registry import SkillRegistry

        reg = SkillRegistry.get_instance()
        out: List[Dict[str, Any]] = []
        for skill_id in reg.get_all_skill_ids():
            connected, _ = reg.check_connection(skill_id, user_id)
            meta = reg.get_skill_metadata(skill_id)
            out.append(_shape_skill_entry(skill_id, meta, connected))
        return truncate_payload({"skills": out}, tool_name="catalog/skills")
    except Exception:
        logger.exception("catalog/skills failed")
        return dict(_INTERNAL_ERROR)


def _slim_incident_row(i: Dict[str, Any]) -> Dict[str, Any]:
    # /api/incidents returns the camelCase shape from _format_incident_response
    # in routes/incidents_routes.py — title/service are nested under `alert`,
    # timestamps are camelCase. Fall back to snake_case keys for forward
    # compatibility if the API shape ever flattens.
    alert = i.get("alert") if isinstance(i.get("alert"), dict) else {}
    return {
        "id": i.get("id"),
        "title": alert.get("title") or i.get("alert_title") or i.get("title"),
        "status": i.get("auroraStatus") or i.get("aurora_status") or i.get("status"),
        "severity": i.get("severity"),
        "service": alert.get("service") or i.get("alert_service"),
        "created_at": i.get("createdAt") or i.get("created_at"),
    }


async def _do_incidents_recent(api_call: ApiCall) -> Dict[str, Any]:
    try:
        data = await api_call("GET", "/api/incidents", params={"limit": 20})
        items = data.get("incidents") if isinstance(data, dict) else data
        if not isinstance(items, list):
            items = []
        slim = [_slim_incident_row(i) for i in items if isinstance(i, dict)]
        return truncate_payload({"incidents": slim}, tool_name="incidents/recent")
    except Exception:
        logger.exception("incidents/recent failed")
        return dict(_INTERNAL_ERROR)


async def _do_runbooks_index(api_call: ApiCall) -> Dict[str, Any]:
    sources = (
        ("knowledge_base", "/api/knowledge-base/documents"),
        ("sharepoint", "/sharepoint/sites"),
    )
    results = await asyncio.gather(
        *(api_call("GET", path) for _, path in sources),
        return_exceptions=True,
    )
    out = []
    for (name, _), res in zip(sources, results):
        if isinstance(res, Exception):
            logger.warning("runbooks/index source %s failed: %s", name, res)
            continue
        out.append({"source": name, "items": res})
    return truncate_payload({"sources": out}, tool_name="runbooks/index")


async def _do_health(api_call: ApiCall) -> Dict[str, Any]:
    try:
        return await api_call("GET", "/health/")
    except Exception:
        logger.exception("health check failed")
        return dict(_INTERNAL_ERROR)


def register_resources(
    mcp,
    api_call: ApiCall,
    get_token: Callable[[], str],
    resolve_token: Callable[[str], Any],
) -> None:

    def _user_id() -> str:
        token = get_token()
        user_id, _ = resolve_token(token)
        return user_id

    def _identity() -> tuple:
        token = get_token()
        return resolve_token(token)

    @mcp.resource("aurora://catalog/connectors")
    async def catalog_connectors() -> Dict[str, Any]:
        """List of the user's connected providers, each annotated with
        mcp_tools_available so clients can tell connect-only providers
        (e.g. AWS) from ones with a real MCP surface."""
        return await _do_catalog_connectors(api_call)

    @mcp.resource("aurora://whoami")
    async def whoami() -> Dict[str, Any]:
        """The user_id and org_id the bearer token resolves to.

        Use this to debug 'why can't I see X' — if the user_id or org_id
        doesn't match what you expect, your token is bound to a different
        identity than the data you're trying to read.
        """
        uid, oid = _identity()
        return _do_whoami(uid, oid)

    @mcp.resource("aurora://catalog/skills")
    async def catalog_skills() -> Dict[str, Any]:
        """Available skills (from the skill registry) with connection status for this user."""
        return _do_catalog_skills(_user_id())

    @mcp.resource("aurora://incidents/recent")
    async def incidents_recent() -> Dict[str, Any]:
        """Last 20 incidents — semantic IDs and titles only (no full bodies)."""
        return await _do_incidents_recent(api_call)

    @mcp.resource("aurora://runbooks/index")
    async def runbooks_index() -> Dict[str, Any]:
        """Discoverable index across connected doc backends. Knowledge base
        documents + SharePoint sites — Confluence has no listing endpoint,
        fetch its pages via call_tool('confluence_fetch_page', { url })."""
        return await _do_runbooks_index(api_call)

    @mcp.resource("aurora://health")
    async def health() -> Dict[str, Any]:
        """Aurora system health: database, Redis, Weaviate, Celery status."""
        return await _do_health(api_call)
