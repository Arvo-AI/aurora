"""Tier-1 always-on MCP tools — focused on the 80% incident workflow.

These tools are registered for every user regardless of which connectors
are wired up. Descriptions are written so a good external LLM reaches for a
direct read (incidents, infrastructure context, service graph, RCA findings)
for factual lookups, and escalates to `chat_with_aurora` only for open-ended,
multi-source investigation/synthesis.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional
from urllib.parse import quote

from .response import truncate_payload
from .chat_bridge import chat_with_aurora as _chat_with_aurora

logger = logging.getLogger(__name__)

ApiCall = Callable[..., Awaitable[Dict[str, Any]]]

# Per-call timeouts (seconds). Defaults that diverge from the shared httpx
# client default live here so they can be tuned in one place.
_ASK_POLL_TIMEOUT = 15.0
_RCA_TRIGGER_TIMEOUT = 60.0

# Terminal states mirror chat_bridge._TERMINAL_*. Use an explicit allowlist so
# unknown/new statuses (e.g. "active") don't get treated as terminal by a
# denylist check.
_TERMINAL_OK = frozenset({"complete", "completed"})
_TERMINAL_ERR = frozenset({"error", "cancelled", "failed"})


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


async def _do_list_incidents(api_call: ApiCall, status: Optional[str], limit: int, offset: int) -> Dict[str, Any]:
    params: Dict[str, Any] = {"limit": limit}
    if status:
        params["status"] = status
    if offset > 0:
        params["offset"] = offset
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
        status = session.get("status")
        if status in _TERMINAL_OK or status in _TERMINAL_ERR:
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
        logger.exception(
            "search_runbooks: knowledge_base call failed", exc_info=kb_res,
        )
        sources.append({"source": "knowledge_base", "error": "search_failed"})
    else:
        sources.append({"source": "knowledge_base", "results": kb_res})
    # SharePoint silently skipped on error — likely not connected; callers
    # should hit the explicit `sharepoint_search` dispatch entry for the error.
    if not isinstance(sp_res, Exception):
        sources.append({"source": "sharepoint", "results": sp_res})

    return truncate_payload({"sources": sources}, tool_name="search_runbooks")


async def _do_get_infrastructure_context(api_call: ApiCall) -> Dict[str, Any]:
    return truncate_payload(
        await api_call("GET", "/api/graph/infrastructure/context"),
        tool_name="get_infrastructure_context",
    )


async def _do_list_services(
    api_call: ApiCall, resource_type: Optional[str], provider: Optional[str],
) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    if resource_type:
        params["resource_type"] = resource_type
    if provider:
        params["provider"] = provider
    return truncate_payload(
        await api_call("GET", "/api/graph/services", params=params or None),
        tool_name="list_services",
    )


async def _do_service_impact(api_call: ApiCall, name: str) -> Dict[str, Any]:
    # First-class helpers bypass dispatch's _build_path/quote, and _api only
    # blocks `..` (not arbitrary chars), so encode the name ourselves — a
    # service name with a "/" or space would otherwise break the path.
    encoded = quote(name, safe="")
    return truncate_payload(
        await api_call("GET", f"/api/graph/services/{encoded}/impact"),
        tool_name="service_impact",
    )


async def _do_incident_findings(api_call: ApiCall, incident_id: str) -> Dict[str, Any]:
    return truncate_payload(
        await api_call("GET", f"/api/incidents/{incident_id}/findings"),
        tool_name="incident_findings",
    )


async def _do_incident_finding_detail(
    api_call: ApiCall, incident_id: str, agent_id: str,
) -> Dict[str, Any]:
    return truncate_payload(
        await api_call("GET", f"/api/incidents/{incident_id}/findings/{agent_id}"),
        tool_name="incident_finding_detail",
    )


async def _do_incident_list_alerts(api_call: ApiCall, incident_id: str) -> Dict[str, Any]:
    return truncate_payload(
        await api_call("GET", f"/api/incidents/{incident_id}/alerts"),
        tool_name="incident_list_alerts",
    )


async def _do_list_actions(api_call: ApiCall) -> Dict[str, Any]:
    return truncate_payload(
        await api_call("GET", "/api/actions"),
        tool_name="list_actions",
    )


async def _do_get_action(api_call: ApiCall, action_id: str) -> Dict[str, Any]:
    return truncate_payload(
        await api_call("GET", f"/api/actions/{action_id}"),
        tool_name="get_action",
    )


async def _do_list_action_runs(
    api_call: ApiCall, action_id: str, limit: int, offset: int,
) -> Dict[str, Any]:
    # Enforce the documented contract here rather than relying on the backend's
    # own clamp, so the tool's stated bounds (1-200, offset >= 0) actually hold.
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    return truncate_payload(
        await api_call(
            "GET", f"/api/actions/{action_id}/runs",
            params={"limit": limit, "offset": offset},
        ),
        tool_name="list_action_runs",
    )


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
        """Aurora's autonomous cloud-ops agent for YOUR connected systems. It both
        INVESTIGATES (multi-source RCA across your incidents and connected
        observability — logs, metrics, traces, alerts) and ACTS (provisions/changes
        infrastructure via Terraform/IaC, kubectl, and cloud CLIs; applies code
        fixes; runs remediations), using Aurora's knowledge of your connected
        integrations. Use it for work needing planning + multi-step action or
        cross-source synthesis — e.g. "why did checkout-svc page at 3am — dig into
        it", "set up auto-scaling for my cluster", "create a GKE cluster". It runs
        the full agent workflow (slower than a direct read), so use it only when the
        task needs the agent, not as a default.

        Do NOT use it for:
        • Simple lookups now covered by direct tools: incidents (latest =
          list_incidents(limit=1)), alerts, topology, metrics, RCA findings, a
          postmortem, or your actions (list_actions) — use those, faster/cheaper.
        • Questions about the Aurora product itself (how the app works, its
          features, UI, settings) — answer from your own knowledge.

        SESSION THREADING — READ FIRST:
        Chats are session-scoped; every call returns a `session_id`. On ANY
        follow-up in the same conversation (clarifications, "what about X?",
        polling) you MUST pass that `session_id` back. Omit it only to start an
        unrelated topic — a fresh session loses all prior context and citations.

        Args:
          message: User question. Ignored when poll_only=True.
          session_id: from the previous result; REQUIRED on every follow-up turn,
            omit only to start a new, unrelated chat.
          mode: "chat" (default) or "rca" for the deeper RCA pipeline.
          poll_only: keep polling a running session (requires session_id).

        Returns: dict with `session_id` (always — keep for the next call), `status`,
          and either `response` + `citations`, `partial` + `hint`, or `error`.
        """
        result = await _chat_with_aurora(
            api_call, message=message, session_id=session_id,
            mode=mode, poll_only=poll_only,
        )
        return truncate_payload(result, tool_name="chat_with_aurora")

    @mcp.tool()
    async def list_incidents(status: Optional[str] = None, limit: int = 20, offset: int = 0) -> Dict[str, Any]:
        """List Aurora incidents. Optionally filter by status
        (investigating/analyzed/merged/resolved).

        Args:
          status: Filter by incident status (optional).
          limit: Max incidents to return (1-100, default 20).
          offset: Paging offset (>= 0, default 0).

        Returns {incidents: [...], total: N}. Use total to know when to
        stop paging (e.g. offset + limit >= total means last page).
        """
        return await _do_list_incidents(api_call, status, limit, offset)

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
        """Ask Aurora a genuine free-text follow-up about a specific incident
        (runs an incident-scoped investigation — slower than a direct read).

        Before calling, for a factual lookup use the direct tool: `get_incident`
        for the summary/citations, `incident_findings` /
        `incident_finding_detail` for which tools/steps the RCA used, and
        `incident_list_alerts` for the correlated alerts. Use this only for
        open-ended questions those reads can't answer. For broader, cross-incident
        investigations use `chat_with_aurora`."""
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

    # The next three tools (get_infrastructure_context, list_services,
    # service_impact) are promoted from the Tier-3 dispatch allowlist to
    # first-class tools. They intentionally shadow the former
    # get_infrastructure_context / graph_list_services / graph_service_impact
    # dispatch entries — do NOT re-add those to DISPATCH_ALLOWLIST (see the NOTE
    # in registry.py); double-exposure is asserted against in tests.
    @mcp.tool()
    async def get_infrastructure_context() -> Dict[str, Any]:
        """Get the system's infrastructure context: environments, services,
        dependencies, CI/CD, and monitoring. Use this FIRST to understand the
        topology before investigating — it's a direct read, no chat needed."""
        return await _do_get_infrastructure_context(api_call)

    @mcp.tool()
    async def list_services(
        resource_type: Optional[str] = None, provider: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List the services in Aurora's dependency graph. Use this to enumerate
        what exists before drilling into a specific service. Optional filters:
        resource_type, provider."""
        return await _do_list_services(api_call, resource_type, provider)

    @mcp.tool()
    async def service_impact(name: str) -> Dict[str, Any]:
        """Get a service's blast radius — the downstream services that depend on
        it. Use this to answer "what breaks if <service> goes down / what depends
        on <service>". Direct read; no chat needed.

        Args:
          name: the service name (as it appears in list_services / the graph).
        """
        return await _do_service_impact(api_call, name)

    @mcp.tool()
    async def incident_findings(incident_id: str) -> Dict[str, Any]:
        """List what each RCA sub-agent investigated for an incident: role,
        tools_used, citations, and follow-ups. Use this to answer "what did the
        RCA look at / which steps did it run" without spending an ask_incident
        chat call. For one agent's full step-by-step trace, follow up with
        `incident_finding_detail`."""
        return await _do_incident_findings(api_call, incident_id)

    @mcp.tool()
    async def incident_finding_detail(incident_id: str, agent_id: str) -> Dict[str, Any]:
        """Get one RCA sub-agent's full finding plus its per-step
        tool_call_history — the exact tools/steps it used during the
        investigation. Get the agent_id from `incident_findings` first.

        Args:
          incident_id: the incident UUID.
          agent_id: the sub-agent id from incident_findings.
        """
        return await _do_incident_finding_detail(api_call, incident_id, agent_id)

    @mcp.tool()
    async def incident_list_alerts(incident_id: str) -> Dict[str, Any]:
        """List the alerts correlated to an incident: source, title, service,
        severity, and correlation score. Use this to answer "what alerts fired
        for incident X". Direct read; no chat needed."""
        return await _do_incident_list_alerts(api_call, incident_id)

    # The next three are Aurora "Actions" (automations) reads, promoted from the
    # dispatch allowlist to first-class tools so "list my actions" maps here
    # directly. The action WRITES (create/update/delete/restore/trigger) stay in
    # DISPATCH_ALLOWLIST (registry.py) — do NOT re-add these reads there.
    @mcp.tool()
    async def list_actions() -> Dict[str, Any]:
        """List this org's Aurora actions (automations): name, trigger type,
        mode, enabled, run count, and last-run status. Use this when the user
        asks to see their actions or automations. Direct read; no chat needed.
        (Aurora "Actions" are saved automations — not the agent's own tools.)"""
        return await _do_list_actions(api_call)

    @mcp.tool()
    async def get_action(action_id: str) -> Dict[str, Any]:
        """Get one Aurora action's full config plus its 20 most recent runs.

        Args:
          action_id: the action UUID (from list_actions).
        """
        return await _do_get_action(api_call, action_id)

    @mcp.tool()
    async def list_action_runs(
        action_id: str, limit: int = 50, offset: int = 0,
    ) -> Dict[str, Any]:
        """List an Aurora action's run history: status, timing, linked incident/
        chat session, and any error. Use this to check whether an automation ran
        and how it went.

        Args:
          action_id: the action UUID (from list_actions).
          limit: max runs (1-200, default 50). offset: paging offset.
        """
        return await _do_list_action_runs(api_call, action_id, limit, offset)
