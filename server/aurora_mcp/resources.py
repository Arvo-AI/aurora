"""MCP resources — URI-fetched data, zero upfront token cost."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, List

from .response import truncate_payload

logger = logging.getLogger(__name__)

ApiCall = Callable[..., Awaitable[Dict[str, Any]]]


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
        try:
            return truncate_payload(
                await api_call("GET", "/api/connectors/status"),
                tool_name="catalog/connectors",
            )
        except Exception as e:
            return {"error": str(e)}

    @mcp.resource("aurora://catalog/skills")
    async def catalog_skills() -> Dict[str, Any]:
        """Available skills (from the skill registry) with connection status for this user."""
        try:
            from chat.backend.agent.skills.registry import SkillRegistry

            user_id = _user_id()
            reg = SkillRegistry.get_instance()
            out: List[Dict[str, Any]] = []
            for skill_id in reg.get_all_skill_ids():
                connected, _ = reg.check_connection(skill_id, user_id)
                meta = reg._skills.get(skill_id)  # noqa: SLF001
                out.append({
                    "id": skill_id,
                    "name": getattr(meta, "name", skill_id),
                    "category": getattr(meta, "category", "") if meta else "",
                    "connected": bool(connected),
                })
            return truncate_payload({"skills": out}, tool_name="catalog/skills")
        except Exception as e:
            return {"error": str(e)}

    @mcp.resource("aurora://incidents/recent")
    async def incidents_recent() -> Dict[str, Any]:
        """Last 20 incidents — semantic IDs and titles only (no full bodies)."""
        try:
            data = await api_call("GET", "/api/incidents", params={"limit": 20})
            items = data.get("incidents") if isinstance(data, dict) else data
            if not isinstance(items, list):
                items = []
            slim = [
                {
                    "id": i.get("id"),
                    "title": i.get("alert_title") or i.get("title"),
                    "status": i.get("aurora_status") or i.get("status"),
                    "severity": i.get("severity"),
                    "service": i.get("alert_service"),
                    "created_at": i.get("created_at"),
                }
                for i in items
                if isinstance(i, dict)
            ]
            return truncate_payload({"incidents": slim}, tool_name="incidents/recent")
        except Exception as e:
            return {"error": str(e)}

    @mcp.resource("aurora://runbooks/index")
    async def runbooks_index() -> Dict[str, Any]:
        """Discoverable index across connected doc backends. Knowledge base
        documents + SharePoint sites — Confluence has no listing endpoint,
        fetch its pages via call_tool('confluence_fetch_page', { url })."""
        sources = (
            ("knowledge_base", "/api/knowledge-base/documents"),
            ("sharepoint", "/sharepoint/sites"),
        )
        results = await asyncio.gather(
            *(api_call("GET", path) for _, path in sources),
            return_exceptions=True,
        )
        out = [
            {"source": name, "items": res}
            for (name, _), res in zip(sources, results)
            if not isinstance(res, Exception)
        ]
        return truncate_payload({"sources": out}, tool_name="runbooks/index")

    @mcp.resource("aurora://health")
    async def health() -> Dict[str, Any]:
        """Aurora system health: database, Redis, Weaviate, Celery status."""
        try:
            return await api_call("GET", "/health/")
        except Exception as e:
            return {"error": str(e)}
