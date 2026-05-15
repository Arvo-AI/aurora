"""Tier-1 always-on MCP tools — focused on the 80% incident workflow.

These tools are registered for every user regardless of which connectors
are wired up. Descriptions are written so a good external LLM will prefer
`chat_with_aurora` for ambiguous investigations and fall back to direct
tools only when the user explicitly asks for raw data.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .response import truncate_payload
from .chat_bridge import chat_with_aurora as _chat_with_aurora

logger = logging.getLogger(__name__)

ApiCall = Callable[..., Awaitable[Dict[str, Any]]]

# Per-call timeouts (seconds). Defaults that diverge from the shared httpx
# client default live here so they can be tuned in one place.
_ASK_POLL_TIMEOUT = 15.0
_RCA_TRIGGER_TIMEOUT = 60.0


def _slim_incident(incident: Any) -> Any:
    if not isinstance(incident, dict):
        return incident
    incident = {k: v for k, v in incident.items() if k != "streamingThoughts"}
    sessions = incident.get("chatSessions")
    if isinstance(sessions, list):
        incident["chatSessions"] = [
            {k: s.get(k) for k in ("id", "title", "status") if k in s}
            for s in sessions if isinstance(s, dict)
        ]
    return incident


async def _do_list_incidents(api_call: ApiCall, status: Optional[str], limit: int) -> Dict[str, Any]:
    params: Dict[str, Any] = {"limit": limit}
    if status:
        params["status"] = status
    return truncate_payload(
        await api_call("GET", "/api/incidents", params=params),
        tool_name="list_incidents",
    )


async def _do_get_incident(api_call: ApiCall, incident_id: str) -> Dict[str, Any]:
    raw = await api_call("GET", f"/api/incidents/{incident_id}")
    if isinstance(raw, dict) and "incident" in raw:
        return truncate_payload(
            {"incident": _slim_incident(raw["incident"])},
            tool_name="get_incident",
        )
    return truncate_payload(_slim_incident(raw), tool_name="get_incident")


async def _do_ask_incident(api_call: ApiCall, incident_id: str, question: str) -> Dict[str, Any]:
    result = await api_call(
        "POST",
        f"/api/incidents/{incident_id}/chat",
        body={"question": question, "mode": "ask"},
    )
    session_id = result.get("session_id") if isinstance(result, dict) else None
    if not session_id:
        return truncate_payload(result, tool_name="ask_incident")

    for _ in range(20):
        await asyncio.sleep(2)
        async with asyncio.timeout(_ASK_POLL_TIMEOUT):
            session = await api_call("GET", f"/chat_api/sessions/{session_id}")
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


async def _do_regenerate_rca(api_call: ApiCall, incident_id: str) -> Dict[str, Any]:
    async with asyncio.timeout(_RCA_TRIGGER_TIMEOUT):
        result = await api_call(
            "POST",
            f"/api/incidents/{incident_id}/postmortem/regenerate",
        )
    return truncate_payload(result, tool_name="regenerate_rca")


async def _do_trigger_rca(
    api_call: ApiCall,
    issue_description: str,
    title: str,
    service: str,
    severity: str,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {"issue_description": issue_description, "severity": severity}
    if title:
        body["title"] = title
    if service:
        body["service"] = service
    async with asyncio.timeout(_RCA_TRIGGER_TIMEOUT):
        result = await api_call("POST", "/api/incidents/trigger-rca", body=body)
    return truncate_payload(result, tool_name="trigger_rca")


async def _do_knowledge_base_search(api_call: ApiCall, query: str, limit: int) -> Dict[str, Any]:
    return truncate_payload(
        await api_call(
            "POST",
            "/api/knowledge-base/search",
            body={"query": query, "limit": limit},
        ),
        tool_name="knowledge_base_search",
    )


async def _do_search_runbooks(api_call: ApiCall, query: str, limit: int) -> Dict[str, Any]:
    kb_call = api_call("POST", "/api/knowledge-base/search",
                       body={"query": query, "limit": limit})
    sp_call = api_call("POST", "/sharepoint/search",
                       body={"query": query, "maxResults": limit})
    kb_res, sp_res = await asyncio.gather(kb_call, sp_call, return_exceptions=True)

    sources: List[Dict[str, Any]] = []
    if isinstance(kb_res, Exception):
        sources.append({"source": "knowledge_base", "error": str(kb_res)})
    else:
        sources.append({"source": "knowledge_base", "results": kb_res})
    # SharePoint silently skipped on error — likely not connected; callers
    # should hit the explicit `sharepoint_search` dispatch entry for the error.
    if not isinstance(sp_res, Exception):
        sources.append({"source": "sharepoint", "results": sp_res})

    return truncate_payload({"sources": sources}, tool_name="search_runbooks")


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

        SESSION THREADING — READ THIS BEFORE CALLING:
        Aurora chats are session-scoped. Every call returns a `session_id` in the
        result. For ANY follow-up turn in the same conversation (clarifications,
        next steps, "and also…", "what about X?", re-asking after a partial
        answer, polling, etc.) you MUST pass that `session_id` back. Omit it
        ONLY when the user clearly starts an unrelated new topic. When in
        doubt, reuse the last `session_id` — Aurora has its own memory and
        will branch naturally; starting a fresh session loses all prior
        context, tools, citations, and is almost always the wrong default.

        Concretely:
          • First turn:  chat_with_aurora(message="…")  → note result.session_id
          • Follow-up:   chat_with_aurora(message="…", session_id="<that id>")
          • Status was "in_progress": chat_with_aurora(session_id="<that id>", poll_only=True)

        Args:
          message: User question. Ignored when poll_only=True.
          session_id: The `session_id` from the previous chat_with_aurora result.
            REQUIRED on every follow-up turn in the same conversation. Omit
            only to deliberately start a new, unrelated chat.
          mode: "chat" (default) or "rca" for the deeper RCA pipeline.
          poll_only: True to resume polling a still-running session without
            sending a new turn. Requires session_id.

        Returns: dict with `session_id` (always — keep this for the next call),
          `status` ("complete" | "in_progress" | "error" | "cancelled" | "failed"),
          and either `response` + `citations` (complete), `partial` + `hint`
          (in_progress), or `error` (terminal failure).
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
        return await _do_list_incidents(api_call, status, limit)

    @mcp.tool()
    async def get_incident(incident_id: str) -> Dict[str, Any]:
        """Get full incident details: summary, suggestions, citations, alerts.

        Strips the large `streamingThoughts` log (agent intermediate reasoning,
        ~44k chars for a typical incident) and slims `chatSessions` to id/title/
        status — those are useful in the Aurora UI but cost too much over MCP.
        Pull the full body via the Aurora UI or a direct API call if needed."""
        return await _do_get_incident(api_call, incident_id)

    @mcp.tool()
    async def ask_incident(incident_id: str, question: str) -> Dict[str, Any]:
        """Ask Aurora a follow-up question about a specific incident. Use this for
        incident-scoped Q&A; for broader investigations use chat_with_aurora."""
        return await _do_ask_incident(api_call, incident_id, question)

    @mcp.tool()
    async def regenerate_rca(incident_id: str) -> Dict[str, Any]:
        """Re-run RCA for an EXISTING incident. Re-investigates and rewrites
        the postmortem with refreshed citations. Use this when an incident
        already exists (e.g. surfaced via list_incidents) and you want a
        fresh pass. To start an RCA from a free-text description with no
        existing incident, use `trigger_rca` instead."""
        return await _do_regenerate_rca(api_call, incident_id)

    @mcp.tool()
    async def trigger_rca(
        issue_description: str,
        title: str = "",
        service: str = "",
        severity: str = "medium",
    ) -> Dict[str, Any]:
        """Start a NEW RCA from a free-text problem description. Creates an
        incident record and dispatches Aurora's full background investigation
        across all connected integrations — the same pipeline used by the
        UI's RCA button and by webhook-triggered alerts. Returns the new
        `incident_id` and a `rca_session_id` to track progress.

        Use this when the user describes an issue and there is no existing
        Aurora incident for it yet. If an incident already exists, prefer
        `regenerate_rca(incident_id)` instead.

        Args:
          issue_description: REQUIRED. What the user is seeing — symptoms,
            timing, affected surface. The agent will use this as the seed.
          title: Optional short title, e.g. "API latency spike". If empty,
            one is derived from `issue_description`.
          service: Optional affected service name if identifiable.
          severity: One of "critical", "high", "medium" (default), "low".
        """
        return await _do_trigger_rca(
            api_call,
            issue_description=issue_description,
            title=title,
            service=service,
            severity=severity,
        )

    @mcp.tool()
    async def knowledge_base_search(query: str, limit: int = 5) -> Dict[str, Any]:
        """Semantic search across Aurora's knowledge base (uploaded docs, indexed runbooks)."""
        return await _do_knowledge_base_search(api_call, query, limit)

    @mcp.tool()
    async def search_runbooks(query: str, limit: int = 5) -> Dict[str, Any]:
        """Search runbooks/docs across the Aurora knowledge base and SharePoint
        (when connected). Confluence has no search endpoint — fetch specific
        Confluence pages via call_tool('confluence_fetch_page', { url })."""
        return await _do_search_runbooks(api_call, query, limit)
