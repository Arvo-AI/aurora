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


async def _do_catalog_connectors(api_call: ApiCall) -> Dict[str, Any]:
    try:
        return truncate_payload(
            await api_call("GET", "/api/connectors/status"),
            tool_name="catalog/connectors",
        )
    except Exception:
        logger.exception("catalog/connectors failed")
        return dict(_INTERNAL_ERROR)


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

    @mcp.resource("aurora://catalog/connectors")
    async def catalog_connectors() -> Dict[str, Any]:
        """List of the user's connected providers and their status."""
        return await _do_catalog_connectors(api_call)

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
