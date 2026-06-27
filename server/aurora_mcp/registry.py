"""Aurora MCP registry — single source of truth for the MCP tool surface.

Three tiers:
  - Tier 1: always advertised, no connector dependency
  - Tier 2: advertised iff at least one enabling skill is connected for the user
            (gated via SkillRegistry.check_connection)
  - Tier 3: not advertised; reachable through search_tools + call_tool

The DISPATCH_ALLOWLIST is the security boundary for Tier 3 — any agent tool
not listed here is unreachable from MCP. Infra-write tool families
(Terraform apply, kubectl mutations, shell-equivalents, Cloudflare WAF/DNS)
are intentionally excluded.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Aurora-internal artifacts API base path, shared by the artifact_* dispatch entries.
_ARTIFACTS_PATH = "/api/artifacts"


# ---------------------------------------------------------------------------
# Connector-status cache — populated via the Flask backend API by the MCP
# server so skill-connectivity checks don't rely on local DB access (which
# can fail due to missing Flask/RLS context in the standalone MCP process).
# ---------------------------------------------------------------------------

_connector_cache: Dict[str, Tuple[float, Dict[str, bool]]] = {}
_CONNECTOR_CACHE_TTL = 30.0  # seconds


def set_connector_status(user_id: str, status: Dict[str, bool]) -> None:
    """Cache connector status for a user (called from mcp_server.py)."""
    _connector_cache[user_id] = (time.monotonic(), status)


def _get_cached_connector_status(user_id: str) -> Optional[Dict[str, bool]]:
    """Return cached connector status if fresh, else None."""
    entry = _connector_cache.get(user_id)
    if entry is None:
        return None
    ts, status = entry
    if time.monotonic() - ts > _CONNECTOR_CACHE_TTL:
        _connector_cache.pop(user_id, None)
        return None
    return status


def parse_and_cache_connector_status(user_id: str, api_response: Dict) -> None:
    """Parse /api/connectors/status response and cache the result.

    Shared by mcp_server._refresh_connector_cache and dispatch._ensure_connector_cache.
    """
    connectors = api_response.get("connectors", {})
    status = {
        provider.lower(): bool(info.get("connected"))
        for provider, info in connectors.items()
        if isinstance(info, dict)
    }
    set_connector_status(user_id, status)


# ---------------------------------------------------------------------------
# Tier 2 — connector-gated tool spec
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GatedToolSpec:
    """A Tier-2 tool that's visible iff at least one enabling skill is connected."""

    name: str
    description: str
    # Skill IDs (from server/chat/backend/agent/skills/integrations/) — any one
    # being connected for the user enables the tool.
    enabling_skills: Tuple[str, ...]
    # category used by search_tools
    category: str = "observability"


TIER2_TOOLS: Tuple[GatedToolSpec, ...] = (
    GatedToolSpec(
        name="query_logs",
        description=(
            "Query logs from any connected log source. Currently backed by "
            "Datadog (/datadog/logs/search) and Splunk (/splunk/search). Use "
            "this for a direct log read."
        ),
        enabling_skills=("datadog", "splunk"),
        category="logs",
    ),
    GatedToolSpec(
        name="query_metrics",
        description=(
            "Query metrics directly. Currently backed by Datadog "
            "(/datadog/metrics/query). Use this for a direct metric read."
        ),
        enabling_skills=("datadog",),
        category="metrics",
    ),
    GatedToolSpec(
        name="query_alerts",
        description=(
            "Read alerts and incident webhooks from connected alerting tools "
            "(Datadog monitors, New Relic issues, Dynatrace alerts, OpsGenie, "
            "incident.io, Splunk)."
        ),
        enabling_skills=(
            "datadog", "newrelic", "dynatrace", "opsgenie", "incidentio", "splunk",
        ),
        category="alerts",
    ),
    GatedToolSpec(
        name="query_jira",
        description=(
            "Search Jira issues (JQL) or fetch a specific issue by key. "
            "Writes available via call_tool (jira_create_issue, jira_add_comment)."
        ),
        enabling_skills=("jira",),
        category="ticketing",
    ),
    GatedToolSpec(
        name="query_notion",
        description=(
            "Notion reads: list connected databases or fetch a single "
            "database's metadata and rows."
        ),
        enabling_skills=("notion",),
        category="docs",
    ),
    GatedToolSpec(
        name="query_bitbucket",
        description=(
            "Bitbucket reads: list workspaces, repos in a workspace, branches "
            "or PRs in a repo. Most actions require a workspace slug; pass "
            "workspace='myco' (the URL slug, not the display name)."
        ),
        enabling_skills=("bitbucket",),
        category="code",
    ),
)


# ---------------------------------------------------------------------------
# Tier 3 — long-tail dispatch allowlist
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DispatchEntry:
    """Long-tail agent tool reachable through call_tool.

    `name` is the user-visible tool identifier exposed via search_tools.
    `api` is the Aurora REST path (method, path) the MCP proxy will call.
    `body_keys` lists args that go in the JSON body (everything else is query).
    `enabling_skills` is the same gating gate as Tier 2 — pass () for always-on.
    """

    name: str
    description: str
    category: str
    method: str
    path: str
    enabling_skills: Tuple[str, ...] = ()
    # Args that must go in the JSON body. Others go in the query string.
    body_keys: Tuple[str, ...] = ()
    # Args that must be substituted into the path (e.g. {issue_key}).
    path_args: Tuple[str, ...] = ()
    # Query-string args to advertise in the tool schema so the LLM knows to
    # pass them (forwarding already routes any non-body/non-path arg to query).
    query_keys: Tuple[str, ...] = ()


# Each entry maps a stable MCP-side name to an existing Aurora REST endpoint.
# Paths here have been verified against server/main_compute.py blueprint
# registration and each connector's routes file. Infra-write families are
# intentionally absent — add new entries here to expose more of the agent
# surface to MCP clients, but do not include Terraform/kubectl/shell/Cloudflare
# WAF surfaces (the assert at import time will reject those).
DISPATCH_ALLOWLIST: Tuple[DispatchEntry, ...] = (
    # ----- Datadog (prefix: /datadog) -----
    DispatchEntry(
        name="datadog_logs_search",
        description="Search Datadog logs by query, time range, and tags.",
        category="logs",
        method="POST",
        path="/datadog/logs/search",
        enabling_skills=("datadog",),
        body_keys=("query", "from", "to", "limit", "indexes"),
    ),
    DispatchEntry(
        name="datadog_metrics_query",
        description="Query Datadog metrics via the query API.",
        category="metrics",
        method="POST",
        path="/datadog/metrics/query",
        enabling_skills=("datadog",),
        body_keys=("query", "from", "to"),
    ),
    DispatchEntry(
        name="datadog_events",
        description="List Datadog events in a time range.",
        category="events",
        method="GET",
        path="/datadog/events",
        enabling_skills=("datadog",),
    ),
    DispatchEntry(
        name="datadog_monitors",
        description="List Datadog monitors and their status.",
        category="alerts",
        method="GET",
        path="/datadog/monitors",
        enabling_skills=("datadog",),
    ),
    # ----- Jira (prefix: /jira) -----
    DispatchEntry(
        name="jira_search_issues",
        description="JQL-based Jira issue search.",
        category="ticketing",
        method="POST",
        path="/jira/search",
        enabling_skills=("jira",),
        body_keys=("jql", "fields", "maxResults"),
    ),
    DispatchEntry(
        name="jira_get_issue",
        description="Fetch a Jira issue by key.",
        category="ticketing",
        method="GET",
        path="/jira/issue/{issue_key}",
        enabling_skills=("jira",),
        path_args=("issue_key",),
    ),
    DispatchEntry(
        name="jira_create_issue",
        description="Create a new Jira issue (write).",
        category="ticketing",
        method="POST",
        path="/jira/issue",
        enabling_skills=("jira",),
        body_keys=("project", "summary", "description", "issuetype", "fields"),
    ),
    DispatchEntry(
        name="jira_add_comment",
        description="Add a comment to a Jira issue (write).",
        category="ticketing",
        method="POST",
        path="/jira/issue/{issue_key}/comment",
        enabling_skills=("jira",),
        path_args=("issue_key",),
        body_keys=("body",),
    ),
    # ----- GitHub (prefix: /github) -----
    DispatchEntry(
        name="github_list_user_repos",
        description="List GitHub repositories the connected user can access.",
        category="code",
        method="GET",
        path="/github/user-repos",
        enabling_skills=("github",),
    ),
    DispatchEntry(
        name="github_list_repo_selections",
        description="List GitHub repos the user has selected as Aurora-connected.",
        category="code",
        method="GET",
        path="/github/repo-selections",
        enabling_skills=("github",),
    ),
    # ----- Splunk (prefix: /splunk) -----
    DispatchEntry(
        name="splunk_search",
        description="Run a Splunk search.",
        category="logs",
        method="POST",
        path="/splunk/search",
        enabling_skills=("splunk",),
        body_keys=("query", "earliest_time", "latest_time", "max_count"),
    ),
    DispatchEntry(
        name="splunk_list_alerts",
        description="List Splunk alerts (ingested via webhook).",
        category="alerts",
        method="GET",
        path="/splunk/alerts",
        enabling_skills=("splunk",),
    ),
    # ----- New Relic / Dynatrace / OpsGenie / incident.io alerts (DB events) -----
    DispatchEntry(
        name="newrelic_list_issues",
        description="List live New Relic issues via NerdGraph.",
        category="alerts",
        method="GET",
        path="/newrelic/issues",
        enabling_skills=("newrelic",),
    ),
    DispatchEntry(
        name="dynatrace_list_alerts",
        description="List Dynatrace alerts (ingested via webhook).",
        category="alerts",
        method="GET",
        path="/dynatrace/alerts",
        enabling_skills=("dynatrace",),
    ),
    DispatchEntry(
        name="opsgenie_list_events",
        description="List OpsGenie alerts that arrived via the ingest webhook.",
        category="alerts",
        method="GET",
        path="/opsgenie/events/ingested",
        enabling_skills=("opsgenie",),
    ),
    DispatchEntry(
        name="incidentio_list_alerts",
        description="List incident.io alerts that arrived via the ingest webhook.",
        category="alerts",
        method="GET",
        path="/incidentio/alerts",
        enabling_skills=("incidentio",),
    ),
    # ----- Notion (prefix: /notion) -----
    DispatchEntry(
        name="notion_list_databases",
        description="List Notion databases the user has connected.",
        category="docs",
        method="GET",
        path="/notion/databases",
        enabling_skills=("notion",),
    ),
    DispatchEntry(
        name="notion_get_database",
        description="Fetch a Notion database by id (metadata + rows).",
        category="docs",
        method="GET",
        path="/notion/databases/{db_id}",
        enabling_skills=("notion",),
        path_args=("db_id",),
    ),
    # ----- Bitbucket (prefix: /bitbucket) — workspace+repo path shape -----
    DispatchEntry(
        name="bitbucket_list_workspaces",
        description="List Bitbucket workspaces the user can access.",
        category="code",
        method="GET",
        path="/bitbucket/workspaces",
        enabling_skills=("bitbucket",),
    ),
    DispatchEntry(
        name="bitbucket_list_projects",
        description="List Bitbucket projects within a workspace.",
        category="code",
        method="GET",
        path="/bitbucket/projects/{workspace}",
        enabling_skills=("bitbucket",),
        path_args=("workspace",),
    ),
    DispatchEntry(
        name="bitbucket_list_repos",
        description="List Bitbucket repos within a workspace.",
        category="code",
        method="GET",
        path="/bitbucket/repos/{workspace}",
        enabling_skills=("bitbucket",),
        path_args=("workspace",),
    ),
    DispatchEntry(
        name="bitbucket_list_branches",
        description="List branches for a Bitbucket repo.",
        category="code",
        method="GET",
        path="/bitbucket/branches/{workspace}/{repo_slug}",
        enabling_skills=("bitbucket",),
        path_args=("workspace", "repo_slug"),
    ),
    DispatchEntry(
        name="bitbucket_list_prs",
        description="List pull requests for a Bitbucket repo.",
        category="code",
        method="GET",
        path="/bitbucket/pull-requests/{workspace}/{repo_slug}",
        enabling_skills=("bitbucket",),
        path_args=("workspace", "repo_slug"),
    ),
    DispatchEntry(
        name="bitbucket_list_issues",
        description="List issues for a Bitbucket repo (issue tracker).",
        category="code",
        method="GET",
        path="/bitbucket/issues/{workspace}/{repo_slug}",
        enabling_skills=("bitbucket",),
        path_args=("workspace", "repo_slug"),
    ),
    # ----- Confluence (prefix: /confluence) — fetch by URL, no search endpoint -----
    DispatchEntry(
        name="confluence_fetch_page",
        description="Fetch a Confluence page's storage-format JSON by URL.",
        category="runbooks",
        method="POST",
        path="/confluence/fetch",
        enabling_skills=("confluence",),
        body_keys=("url", "pageUrl", "page_url"),
    ),
    DispatchEntry(
        name="confluence_parse_runbook",
        description="Fetch a Confluence page and return cleaned runbook sections.",
        category="runbooks",
        method="POST",
        path="/confluence/parse",
        enabling_skills=("confluence",),
        body_keys=("url", "pageUrl", "page_url"),
    ),
    # ----- SharePoint (prefix: /sharepoint) -----
    DispatchEntry(
        name="sharepoint_search",
        description="Search SharePoint for documents and runbooks.",
        category="runbooks",
        method="POST",
        path="/sharepoint/search",
        enabling_skills=("sharepoint",),
        body_keys=("query", "siteId", "maxResults"),
    ),
    DispatchEntry(
        name="sharepoint_fetch_page",
        description="Fetch a SharePoint page as markdown.",
        category="runbooks",
        method="POST",
        path="/sharepoint/fetch-page",
        enabling_skills=("sharepoint",),
        body_keys=("siteId", "pageId"),
    ),
    DispatchEntry(
        name="sharepoint_fetch_document",
        description="Fetch a SharePoint document as extracted text.",
        category="runbooks",
        method="POST",
        path="/sharepoint/fetch-document",
        enabling_skills=("sharepoint",),
        body_keys=("driveId", "itemId"),
    ),
    DispatchEntry(
        name="sharepoint_list_sites",
        description="List SharePoint sites visible to the connected user.",
        category="runbooks",
        method="GET",
        path="/sharepoint/sites",
        enabling_skills=("sharepoint",),
    ),
    # ----- Postmortems (Aurora-internal — no prefix; /api/incidents/... and /api/postmortems) -----
    DispatchEntry(
        name="postmortem_list",
        description="List postmortems for the org.",
        category="incidents",
        method="GET",
        path="/api/postmortems",
        enabling_skills=(),
    ),
    DispatchEntry(
        name="postmortem_get_for_incident",
        description="Fetch the postmortem for a specific incident.",
        category="incidents",
        method="GET",
        path="/api/incidents/{incident_id}/postmortem",
        enabling_skills=(),
        path_args=("incident_id",),
    ),
    DispatchEntry(
        name="postmortem_regenerate",
        description="Re-trigger postmortem generation for an incident.",
        category="incidents",
        method="POST",
        path="/api/incidents/{incident_id}/postmortem/regenerate",
        enabling_skills=(),
        path_args=("incident_id",),
    ),
    DispatchEntry(
        name="postmortem_update",
        description="Update the postmortem content for an incident (markdown).",
        category="incidents",
        method="PATCH",
        path="/api/incidents/{incident_id}/postmortem",
        enabling_skills=(),
        path_args=("incident_id",),
        body_keys=("content",),
    ),
    DispatchEntry(
        name="postmortem_restore_version",
        description="Restore a previous postmortem version as the current one.",
        category="incidents",
        method="POST",
        path="/api/incidents/{incident_id}/postmortem/versions/{version_id}/restore",
        enabling_skills=(),
        path_args=("incident_id", "version_id"),
    ),
    DispatchEntry(
        name="postmortem_export_confluence",
        description="Export an incident's postmortem to Confluence.",
        category="incidents",
        method="POST",
        path="/api/incidents/{incident_id}/postmortem/export/confluence",
        enabling_skills=("confluence",),
        path_args=("incident_id",),
        body_keys=("spaceKey", "parentPageId"),
    ),
    DispatchEntry(
        name="postmortem_export_jira",
        description="Export an incident's postmortem to Jira as issue + subtasks.",
        category="incidents",
        method="POST",
        path="/api/incidents/{incident_id}/postmortem/export/jira",
        enabling_skills=("jira",),
        path_args=("incident_id",),
        body_keys=("projectKey", "issueType"),
    ),
    DispatchEntry(
        name="postmortem_export_notion",
        description="Export an incident's postmortem to Notion.",
        category="incidents",
        method="POST",
        path="/api/incidents/{incident_id}/postmortem/export/notion",
        enabling_skills=("notion",),
        path_args=("incident_id",),
        body_keys=("databaseId", "titleProperty", "propertyMapping", "actionItemsDatabaseId"),
    ),
    # ----- Artifacts (Aurora-internal — no prefix; /api/artifacts) -----
    DispatchEntry(
        name="artifact_list",
        description="List persistent markdown artifacts for this workspace.",
        category="docs",
        method="GET",
        path=_ARTIFACTS_PATH,
        enabling_skills=(),
    ),
    DispatchEntry(
        name="artifact_read",
        description="Read one artifact's full content by exact title (pass the `title` arg).",
        category="docs",
        method="GET",
        path=_ARTIFACTS_PATH,
        enabling_skills=(),
        query_keys=("title",),
    ),
    DispatchEntry(
        name="artifact_write",
        description="Create or update a persistent markdown artifact by title.",
        category="docs",
        method="POST",
        path=_ARTIFACTS_PATH,
        enabling_skills=(),
        body_keys=("title", "content"),
    ),
    # ----- Incidents — Aurora-internal reads and lifecycle writes -----
    DispatchEntry(
        name="incident_update",
        description="Update incident fields (e.g. status, summary).",
        category="incidents",
        method="PATCH",
        path="/api/incidents/{incident_id}",
        enabling_skills=(),
        path_args=("incident_id",),
        # Mirrors the PATCH route in routes/incidents_routes.py — severity/title
        # are NOT accepted there and would be silently dropped (or trip the
        # "No valid fields to update" 400 if sent alone).
        body_keys=("status", "auroraStatus", "summary", "activeTab"),
    ),
    DispatchEntry(
        name="incident_list_recent_unlinked",
        description="List recent incidents not yet linked to an alert.",
        category="incidents",
        method="GET",
        path="/api/incidents/recent-unlinked",
        enabling_skills=(),
    ),
    DispatchEntry(
        name="incident_submit_feedback",
        description="Submit feedback on the RCA/postmortem of an incident.",
        category="incidents",
        method="POST",
        path="/api/incidents/{incident_id}/feedback",
        enabling_skills=(),
        path_args=("incident_id",),
        body_keys=("rating", "comment", "category"),
    ),
    DispatchEntry(
        name="incident_merge_alert",
        description="Merge an alert into an existing incident.",
        category="incidents",
        method="POST",
        path="/api/incidents/{target_incident_id}/merge-alert",
        enabling_skills=(),
        path_args=("target_incident_id",),
        # The route only reads `sourceIncidentId` (camelCase) from the body.
        # `alert_id` and snake_case `source_incident_id` would be ignored.
        body_keys=("sourceIncidentId",),
    ),
    DispatchEntry(
        name="incident_suggestion_apply",
        description="Apply an AI-generated suggestion (triggers a follow-up action).",
        category="incidents",
        method="POST",
        path="/api/incidents/suggestions/{suggestion_id}/apply",
        enabling_skills=(),
        path_args=("suggestion_id",),
    ),
    DispatchEntry(
        name="incident_suggestion_mark_executed",
        description="Mark an incident suggestion as executed (audit trail).",
        category="incidents",
        method="POST",
        path="/api/incidents/suggestions/{suggestion_id}/mark-executed",
        enabling_skills=(),
        path_args=("suggestion_id",),
    ),
    # ----- Knowledge base reads -----
    DispatchEntry(
        name="kb_get_memory",
        description="Read the org's persistent memory / context content.",
        category="docs",
        method="GET",
        path="/api/knowledge-base/memory",
        enabling_skills=(),
    ),
    DispatchEntry(
        name="kb_get_document",
        description="Fetch a knowledge-base document's metadata and status.",
        category="docs",
        method="GET",
        path="/api/knowledge-base/documents/{doc_id}",
        enabling_skills=(),
        path_args=("doc_id",),
    ),
    # ----- Splunk async search workflow -----
    DispatchEntry(
        name="splunk_create_search_job",
        description="Start an async Splunk search job. Returns a sid you poll with splunk_get_search_job_results.",
        category="logs",
        method="POST",
        path="/splunk/search/jobs",
        enabling_skills=("splunk",),
        body_keys=("query", "earliest_time", "latest_time", "max_count"),
    ),
    DispatchEntry(
        name="splunk_get_search_job",
        description="Get status of an async Splunk search job.",
        category="logs",
        method="GET",
        path="/splunk/search/jobs/{sid}",
        enabling_skills=("splunk",),
        path_args=("sid",),
    ),
    DispatchEntry(
        name="splunk_get_search_job_results",
        description="Get results of a completed async Splunk search job.",
        category="logs",
        method="GET",
        path="/splunk/search/jobs/{sid}/results",
        enabling_skills=("splunk",),
        path_args=("sid",),
    ),
    # ----- Jira extras -----
    DispatchEntry(
        name="jira_update_issue",
        description="Update fields on an existing Jira issue.",
        category="ticketing",
        method="PATCH",
        path="/jira/issue/{issue_key}",
        enabling_skills=("jira",),
        path_args=("issue_key",),
        body_keys=("fields", "summary", "description"),
    ),
    DispatchEntry(
        name="jira_link_issues",
        description="Create a link between two Jira issues.",
        category="ticketing",
        method="POST",
        path="/jira/issue/link",
        enabling_skills=("jira",),
        body_keys=("inwardIssue", "outwardIssue", "type"),
    ),
    # ----- GitHub extras -----
    DispatchEntry(
        name="github_list_branches",
        description="List branches for a GitHub repository.",
        category="code",
        method="GET",
        path="/github/user-branches/{repo_full_name}",
        enabling_skills=("github",),
        path_args=("repo_full_name",),
    ),
    # ----- Service dependency graph (Aurora-internal, /api/graph) -----
    # NOTE: get_infrastructure_context, graph_list_services, and
    # graph_service_impact are promoted to first-class Tier-1 tools (see
    # tools_always_on.py) — exposed there, not here, to avoid double-exposure.
    DispatchEntry(
        name="graph_get_full",
        description="Fetch the full service dependency graph (nodes + edges + stats).",
        category="topology",
        method="GET",
        path="/api/graph",
        enabling_skills=(),
    ),
    DispatchEntry(
        name="graph_get_service",
        description="Fetch a single service with its dependencies.",
        category="topology",
        method="GET",
        path="/api/graph/services/{name}",
        enabling_skills=(),
        path_args=("name",),
    ),
    # ----- LLM usage / cost (Aurora-internal, /api/llm-usage) -----
    DispatchEntry(
        name="llm_usage_summary",
        description="Aggregate LLM usage + cost summary for the org. Query: period=7d|30d|90d|180d|365d.",
        category="usage",
        method="GET",
        path="/api/llm-usage/summary",
        enabling_skills=(),
    ),
    DispatchEntry(
        name="llm_usage_cost_over_time",
        description="LLM cost / token aggregates over time. Query: period, group_by=model|provider, granularity=hour|day|week.",
        category="usage",
        method="GET",
        path="/api/llm-usage/cost-over-time",
        enabling_skills=(),
    ),
    # ----- DORA / SRE metrics (Aurora-internal, /api/metrics) -----
    DispatchEntry(
        name="metrics_summary",
        description="Dashboard overview of incident metrics. Query: period, window_hours.",
        category="metrics",
        method="GET",
        path="/api/metrics/summary",
        enabling_skills=(),
    ),
    DispatchEntry(
        name="metrics_mttr",
        description="Mean Time to Resolve. Query: period, severity, service.",
        category="metrics",
        method="GET",
        path="/api/metrics/mttr",
        enabling_skills=(),
    ),
    DispatchEntry(
        name="metrics_mtts",
        description="Mean Time to Solution (Aurora analysis latency). Query: period, severity, service.",
        category="metrics",
        method="GET",
        path="/api/metrics/mtts",
        enabling_skills=(),
    ),
    DispatchEntry(
        name="metrics_mttd",
        description="Mean Time to Detect. Query: period, severity, service.",
        category="metrics",
        method="GET",
        path="/api/metrics/mttd",
        enabling_skills=(),
    ),
    DispatchEntry(
        name="metrics_incident_frequency",
        description="Incident frequency over time. Query: period and optional grouping.",
        category="metrics",
        method="GET",
        path="/api/metrics/incident-frequency",
        enabling_skills=(),
    ),
    DispatchEntry(
        name="metrics_change_failure_rate",
        description="Change failure rate over the requested period. Query: period.",
        category="metrics",
        method="GET",
        path="/api/metrics/change-failure-rate",
        enabling_skills=(),
    ),
    DispatchEntry(
        name="metrics_agent_execution",
        description="Aurora agent execution metrics over time. Query: period.",
        category="metrics",
        method="GET",
        path="/api/metrics/agent-execution",
        enabling_skills=(),
    ),
    # ----- Actions (Aurora-internal automations, /api/actions) -----
    # NOTE: the reads (list_actions, get_action, list_action_runs) are promoted
    # to first-class Tier-1 tools (see tools_always_on.py) — exposed there, not
    # here, to avoid double-exposure. Only the WRITES live in dispatch.
    DispatchEntry(
        name="action_create",
        description=(
            "Create a new action (write). trigger_type one of manual/on_schedule/"
            "on_incident; on_schedule needs trigger_config.interval_seconds >= 300."
        ),
        category="actions",
        method="POST",
        path="/api/actions",
        enabling_skills=(),
        body_keys=("name", "description", "instructions", "trigger_type", "trigger_config", "mode"),
    ),
    DispatchEntry(
        name="action_update",
        description="Update an action's fields (write). Send only the fields to change.",
        category="actions",
        method="PUT",
        path="/api/actions/{action_id}",
        enabling_skills=(),
        path_args=("action_id",),
        body_keys=("name", "description", "instructions", "trigger_type", "trigger_config", "mode", "enabled"),
    ),
    DispatchEntry(
        name="action_delete",
        description="Delete a user-created action (write). System actions cannot be deleted.",
        category="actions",
        method="DELETE",
        path="/api/actions/{action_id}",
        enabling_skills=(),
        path_args=("action_id",),
    ),
    DispatchEntry(
        name="action_restore_default",
        description="Restore a system action's instructions to the built-in default (write).",
        category="actions",
        method="POST",
        path="/api/actions/{action_id}/restore-default",
        enabling_skills=(),
        path_args=("action_id",),
    ),
    DispatchEntry(
        name="action_trigger",
        description=(
            "Run an action now (write — executes the automation). Optional body: "
            "incident_id (required for on_incident actions), trigger_label."
        ),
        category="actions",
        method="POST",
        path="/api/actions/{action_id}/trigger",
        enabling_skills=(),
        path_args=("action_id",),
        body_keys=("incident_id", "trigger_label"),
    ),
    # ----- CI/CD deployments (Jenkins / CloudBees / Spinnaker) -----
    DispatchEntry(
        name="jenkins_list_deployments",
        description="List recent Jenkins deployments. Query: limit, offset, service.",
        category="cicd",
        method="GET",
        path="/jenkins/deployments",
        enabling_skills=("jenkins",),
    ),
    DispatchEntry(
        name="cloudbees_list_deployments",
        description="List recent CloudBees deployments. Query: limit, offset, service.",
        category="cicd",
        method="GET",
        path="/cloudbees/deployments",
        enabling_skills=("cloudbees",),
    ),
    DispatchEntry(
        name="spinnaker_list_deployments",
        description="List recent Spinnaker deployments. Query: limit, offset, application.",
        category="cicd",
        method="GET",
        path="/spinnaker/deployments",
        enabling_skills=("spinnaker",),
    ),
    DispatchEntry(
        name="spinnaker_list_applications",
        description="List Spinnaker applications.",
        category="cicd",
        method="GET",
        path="/spinnaker/applications",
        enabling_skills=("spinnaker",),
    ),
    DispatchEntry(
        name="spinnaker_list_pipelines",
        description="List recent pipeline executions for a Spinnaker application. Query: limit, statuses.",
        category="cicd",
        method="GET",
        path="/spinnaker/applications/{app}/pipelines",
        enabling_skills=("spinnaker",),
        path_args=("app",),
    ),
    DispatchEntry(
        name="spinnaker_list_pipeline_configs",
        description="List pipeline configurations for a Spinnaker application.",
        category="cicd",
        method="GET",
        path="/spinnaker/applications/{app}/pipeline-configs",
        enabling_skills=("spinnaker",),
        path_args=("app",),
    ),
    DispatchEntry(
        name="spinnaker_app_health",
        description="Health of a Spinnaker application's server groups / clusters.",
        category="cicd",
        method="GET",
        path="/spinnaker/applications/{app}/health",
        enabling_skills=("spinnaker",),
        path_args=("app",),
    ),
    # ----- Sentry (errors / issues) -----
    DispatchEntry(
        name="sentry_list_projects",
        description="List Sentry projects for the connected org.",
        category="monitoring",
        method="GET",
        path="/sentry/projects",
        enabling_skills=("sentry",),
    ),
    DispatchEntry(
        name="sentry_list_issues",
        description="List Sentry issues. Query: query, statsPeriod, project, environment, limit, cursor.",
        category="monitoring",
        method="GET",
        path="/sentry/issues",
        enabling_skills=("sentry",),
    ),
    DispatchEntry(
        name="sentry_list_events",
        description="List Sentry events ingested via webhook. Query: limit, offset, resource.",
        category="monitoring",
        method="GET",
        path="/sentry/events/ingested",
        enabling_skills=("sentry",),
    ),
    # ----- Grafana (alerts) -----
    DispatchEntry(
        name="grafana_list_alerts",
        description="List Grafana alerts ingested via webhook. Query: limit, offset, state.",
        category="alerts",
        method="GET",
        path="/grafana/alerts",
        enabling_skills=("grafana",),
    ),
)


# ---------------------------------------------------------------------------
# Hard-banned tool name fragments — defense-in-depth against accidental
# inclusion of infra-write surfaces in DISPATCH_ALLOWLIST.
# ---------------------------------------------------------------------------

_BANNED_NAME_FRAGMENTS: Tuple[str, ...] = (
    "terraform",
    "iac_apply",
    "iac_destroy",
    # kubectl_get* is the only read-safe kubectl verb; everything else mutates.
    # Enforced by the kubectl_ prefix rule in assert_allowlist_safe.
    "terminal_exec",
    "cloud_exec",
    "tailscale_ssh",
    "cloudflare_action",
    "cloudflare_waf",
    "cloudflare_dns_write",
    "shell_exec",
    "shell_run",
    "shell_command",
)


def _is_banned_kubectl(name: str) -> bool:
    """Any kubectl_ tool except kubectl_get* is banned (apply, delete, exec,
    patch, scale, rollout, drain, cordon, taint, …)."""
    return name.startswith("kubectl_") and not name.startswith("kubectl_get")


def assert_allowlist_safe() -> None:
    """Raise if DISPATCH_ALLOWLIST contains a banned name.

    Called once at import time so any accidental inclusion of an infra-write
    tool fails loudly at startup rather than at request time.
    """
    for entry in DISPATCH_ALLOWLIST:
        lname = entry.name.lower()
        if _is_banned_kubectl(lname):
            raise RuntimeError(
                f"DISPATCH_ALLOWLIST contains banned kubectl write tool "
                f"'{entry.name}'. Only kubectl_get* is read-safe."
            )
        for frag in _BANNED_NAME_FRAGMENTS:
            if frag in lname:
                raise RuntimeError(
                    f"DISPATCH_ALLOWLIST contains banned tool '{entry.name}' "
                    f"(matched fragment '{frag}'). Infra-write tools must not "
                    f"be reachable from MCP. See server/aurora_mcp/registry.py."
                )


assert_allowlist_safe()


# ---------------------------------------------------------------------------
# Per-user visibility — uses SkillRegistry.check_connection
# ---------------------------------------------------------------------------

def _check_skill_connected(skill_id: str, user_id: str) -> bool:
    """Return True iff `skill_id` is connected for `user_id`.

    First checks the connector-status cache (populated from the Flask backend
    API which handles RLS correctly). Falls back to the local SkillRegistry
    check if the cache is empty (e.g. first request before list_tools fires).
    """
    cached = _get_cached_connector_status(user_id)
    if cached is not None:
        return cached.get(skill_id, False)

    try:
        from chat.backend.agent.skills.registry import SkillRegistry

        is_connected, _ = SkillRegistry.get_instance().check_connection(skill_id, user_id)
        return bool(is_connected)
    except Exception:
        logger.warning(
            "skill connection check failed for skill_id=%s",
            skill_id, exc_info=True,
        )
        return False


def gated_tool_visible(spec: GatedToolSpec, user_id: str) -> bool:
    """A Tier-2 tool is visible iff ANY enabling skill is connected."""
    return any(_check_skill_connected(s, user_id) for s in spec.enabling_skills)


def dispatch_entry_visible(entry: DispatchEntry, user_id: str) -> bool:
    """A Tier-3 entry is visible iff it's always-on OR at least one enabler is connected."""
    if not entry.enabling_skills:
        return True
    return any(_check_skill_connected(s, user_id) for s in entry.enabling_skills)


def find_dispatch_entry(name: str) -> Optional[DispatchEntry]:
    for entry in DISPATCH_ALLOWLIST:
        if entry.name == name:
            return entry
    return None


_ALL_SKILLS = frozenset(
    s.lower() for e in DISPATCH_ALLOWLIST for s in e.enabling_skills
)
_ALL_CATEGORIES = frozenset(e.category.lower() for e in DISPATCH_ALLOWLIST)


def _normalize_search_filters(
    query: str, category: Optional[str], connector: Optional[str],
) -> Tuple[str, str, str]:
    q = query.strip().lower()
    cat = (category or "").strip().lower()
    conn = (connector or "").strip().lower()
    if cat and cat not in _ALL_CATEGORIES and cat in _ALL_SKILLS:
        conn = conn or cat
        cat = ""
    return q, cat, conn


def _tokenize_query(q: str) -> List[str]:
    """Split a search query into tokens, dropping fragments shorter than 3
    chars. Splits on any non-alphanumeric run so punctuation-attached tokens
    ("deployments?", "mttr/dora", "rca,tools") separate correctly. A query like
    "rca tools steps" becomes ["rca", "tools", "steps"] so each token can be
    matched independently — the whole-string substring match used previously
    found nothing for such queries and forced a fallback to chat."""
    return [t for t in re.findall(r"[a-z0-9]+", q.lower()) if len(t) >= 3]


def _entry_passes_filters(
    entry: "DispatchEntry", cat: str, conn: str, user_id: Optional[str],
) -> bool:
    """Hard filters independent of the free-text query (category, connector,
    per-user visibility)."""
    if cat and entry.category.lower() != cat:
        return False
    if conn and conn not in {s.lower() for s in entry.enabling_skills}:
        return False
    if user_id is not None and not dispatch_entry_visible(entry, user_id):
        return False
    return True


def _entry_token_match_count(entry: "DispatchEntry", tokens: List[str]) -> int:
    """How many query tokens appear in the entry's name or description.

    Returns 0 when `tokens` is empty (the no-query case), which the caller
    treats as "matches every entry that passes the hard filters"."""
    if not tokens:
        return 0
    haystack = (entry.name + " " + entry.description).lower()
    return sum(1 for t in tokens if t in haystack)


def search_dispatch_entries(
    query: str = "",
    category: Optional[str] = None,
    connector: Optional[str] = None,
    user_id: Optional[str] = None,
    limit: int = 10,
) -> List[DispatchEntry]:
    """Return up to `limit` matching entries, best matches first.

    The free-text `query` is tokenized; an entry matches if ANY token is a
    substring of its name or description, and entries are ranked by the number
    of matching tokens (more matches first). An empty query returns all entries
    that pass the category/connector/visibility filters, up to `limit`.

    If `user_id` is provided, results are filtered to entries the user can
    actually call (enabling skills connected). If the caller passes a value
    in `category` that turns out to match an enabling skill instead of a
    category (a common LLM mistake), treat it as `connector` so the search
    still finds something.
    """
    q, cat, conn = _normalize_search_filters(query, category, connector)
    tokens = _tokenize_query(q)
    results = _ranked_matches(tokens, cat, conn, user_id, limit)
    # Graceful fallback: an LLM frequently guesses a plausible-but-wrong
    # `category` (e.g. category="metrics" for an LLM-usage/cost question whose
    # tools live under category="usage"). When a category/connector filter
    # combined with a real query returns nothing, retry on the query alone so a
    # bad filter guess can't hide a strong token match — otherwise the agent
    # sees zero results and falls back to chat. Empty-query searches keep their
    # explicit scoping (no fallback).
    if not results and tokens and (cat or conn):
        results = _ranked_matches(tokens, "", "", user_id, limit)
    return results


def _ranked_matches(
    tokens: List[str], cat: str, conn: str, user_id: Optional[str], limit: int,
) -> List[DispatchEntry]:
    """Rank allowlist entries by token-match count under the given filters.

    Collects ALL matches first (cannot break early at `limit`) so the
    match-count ranking is computed over the full candidate set before
    truncation. The allowlist is small (~70 entries) so this is cheap.
    """
    scored: List[Tuple[int, int, DispatchEntry]] = []
    for idx, entry in enumerate(DISPATCH_ALLOWLIST):
        if not _entry_passes_filters(entry, cat, conn, user_id):
            continue
        score = _entry_token_match_count(entry, tokens)
        # With a non-empty query, require at least one matching token.
        if tokens and score == 0:
            continue
        # Sort by score desc; idx asc as a stable tie-breaker (preserves the
        # original allowlist order among equally-ranked entries).
        scored.append((-score, idx, entry))
    scored.sort()
    return [entry for _, _, entry in scored[:limit]]
