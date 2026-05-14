"""Tier-1 always-on MCP tools — focused on the 80% incident workflow.

These tools are registered for every user regardless of which connectors
are wired up. Descriptions are written so a good external LLM will prefer
`chat_with_aurora` for ambiguous investigations and fall back to direct
tools only when the user explicitly asks for raw data.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, Optional

from .response import truncate_payload
from .chat_bridge import chat_with_aurora as _chat_with_aurora

logger = logging.getLogger(__name__)

ApiCall = Callable[..., Awaitable[Dict[str, Any]]]


def register_tier1_tools(mcp, api_call: ApiCall) -> None:
    """Register Tier-1 tools on a FastMCP instance.

    `api_call` is the bound `_api(method, path, ...)` from mcp_server.py —
    it forwards user identity from the MCP bearer token.
    """

    @mcp.tool()
    async def chat_with_aurora(
        message: str = "",
        session_id: Optional[str] = None,
        mode: str = "chat",
        poll_only: bool = False,
    ) -> Dict[str, Any]:
        """Default tool for any question about incidents, services, infrastructure, or
        operations. Aurora's agent picks the right data sources, runs RCAs, and cites
        sources. Prefer this over calling individual tools unless the user explicitly
        asks for raw data from a specific source.

        Args:
          message: User question. Ignored when poll_only=True.
          session_id: Continue an existing chat. Omit on the first call.
          mode: "chat" (default) or "rca" for the deeper RCA pipeline.
          poll_only: True to resume polling a still-running session without sending
            a new turn. Requires session_id.
        """
        result = await _chat_with_aurora(
            api_call, message=message, session_id=session_id,
            mode=mode, poll_only=poll_only,
        )
        return truncate_payload(result, tool_name="chat_with_aurora")

    @mcp.tool()
    async def list_incidents(status: Optional[str] = None, limit: int = 20) -> Dict[str, Any]:
        """List Aurora incidents. Optionally filter by status
        (investigating/analyzed/merged/resolved)."""
        params: Dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        return truncate_payload(
            await api_call("GET", "/api/incidents", params=params),
            tool_name="list_incidents",
        )

    @mcp.tool()
    async def get_incident(incident_id: str) -> Dict[str, Any]:
        """Get full incident details: summary, suggestions, citations, alerts.

        Strips the large `streamingThoughts` log (agent intermediate reasoning,
        ~44k chars for a typical incident) and slims `chatSessions` to id/title/
        status — those are useful in the Aurora UI but cost too much over MCP.
        Pull the full body via the Aurora UI or a direct API call if needed."""
        raw = await api_call("GET", f"/api/incidents/{incident_id}")
        incident = raw.get("incident") if isinstance(raw, dict) and "incident" in raw else raw
        if isinstance(incident, dict):
            incident = {k: v for k, v in incident.items() if k != "streamingThoughts"}
            sessions = incident.get("chatSessions")
            if isinstance(sessions, list):
                incident["chatSessions"] = [
                    {k: s.get(k) for k in ("id", "title", "status") if k in s}
                    for s in sessions if isinstance(s, dict)
                ]
        return truncate_payload(
            {"incident": incident} if isinstance(raw, dict) and "incident" in raw else incident,
            tool_name="get_incident",
        )

    @mcp.tool()
    async def ask_incident(incident_id: str, question: str) -> Dict[str, Any]:
        """Ask Aurora a follow-up question about a specific incident. Use this for
        incident-scoped Q&A; for broader investigations use chat_with_aurora."""
        result = await api_call(
            "POST",
            f"/api/incidents/{incident_id}/chat",
            body={"question": question, "mode": "ask"},
            timeout=30,
        )
        session_id = result.get("session_id")
        if not session_id:
            return truncate_payload(result, tool_name="ask_incident")

        for _ in range(20):
            await asyncio.sleep(2)
            session = await api_call(
                "GET", f"/chat_api/sessions/{session_id}", timeout=15
            )
            if session.get("status") not in ("processing", "pending", "in_progress"):
                return truncate_payload(session, tool_name="ask_incident")

        return {
            "status": "still_processing",
            "session_id": session_id,
            "message": (
                "Response not ready after 40s. Re-call with chat_with_aurora "
                f"(session_id={session_id}) or read the session directly."
            ),
        }

    @mcp.tool()
    async def trigger_rca(incident_id: str) -> Dict[str, Any]:
        """Re-trigger Aurora's RCA/postmortem pipeline for an incident. Aurora
        will re-run the investigation and rewrite the postmortem with refreshed
        citations. Backed by the postmortem regenerate route — Aurora's RCA
        pipeline writes its output into the postmortem, so regenerating the
        postmortem effectively re-runs the RCA."""
        return truncate_payload(
            await api_call(
                "POST",
                f"/api/incidents/{incident_id}/postmortem/regenerate",
                timeout=60,
            ),
            tool_name="trigger_rca",
        )

    @mcp.tool()
    async def knowledge_base_search(query: str, limit: int = 5) -> Dict[str, Any]:
        """Semantic search across Aurora's knowledge base (uploaded docs, indexed runbooks)."""
        return truncate_payload(
            await api_call(
                "POST",
                "/api/knowledge-base/search",
                body={"query": query, "limit": limit},
            ),
            tool_name="knowledge_base_search",
        )

    @mcp.tool()
    async def search_runbooks(query: str, limit: int = 5) -> Dict[str, Any]:
        """Search runbooks/docs across the Aurora knowledge base and SharePoint
        (when connected). Confluence has no search endpoint — fetch specific
        Confluence pages via call_tool('confluence_fetch_page', { url })."""
        kb_call = api_call("POST", "/api/knowledge-base/search",
                           body={"query": query, "limit": limit})
        sp_call = api_call("POST", "/sharepoint/search",
                           body={"query": query, "maxResults": limit}, timeout=20)
        kb_res, sp_res = await asyncio.gather(kb_call, sp_call, return_exceptions=True)

        sources = []
        if isinstance(kb_res, Exception):
            sources.append({"source": "knowledge_base", "error": str(kb_res)})
        else:
            sources.append({"source": "knowledge_base", "results": kb_res})
        # SharePoint silently skipped on error — likely not connected; callers
        # should hit the explicit `sharepoint_search` dispatch entry for the error.
        if not isinstance(sp_res, Exception):
            sources.append({"source": "sharepoint", "results": sp_res})

        return truncate_payload({"sources": sources}, tool_name="search_runbooks")
