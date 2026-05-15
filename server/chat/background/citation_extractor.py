"""Extract citations from RCA chat sessions for evidence linking."""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Dict, Any

from chat.backend.agent.orchestrator.dispatcher import DISPATCH_SUBAGENT_TOOL_NAME

logger = logging.getLogger(__name__)

# Display-friendly names for the citation modal. Hoisted to module scope so we
# don't rebuild the dict per call.
_TOOL_NAME_MAPPING: Dict[str, str] = {
    "bash": "Terminal",
    "run_bash_command": "Terminal",
    "terminal_exec": "Terminal",
    "read_file": "File Read",
    "write_file": "File Write",
    "search_code": "Code Search",
    "execute_kubectl": "kubectl",
    "on_prem_kubectl": "kubectl",
    "kubectl_onprem": "kubectl",
    "gcloud_command": "gcloud",
    "aws_command": "AWS CLI",
    "azure_command": "Azure CLI",
    "cloud_exec": "Cloud CLI",  # Unified cloud command tool (gcloud/aws/azure)
    "iac_tool": "Terraform",
    "github_commit": "GitHub",
    "github_rca": "GitHub RCA",
    "github_fix": "GitHub Fix",
    "github_apply_fix": "GitHub Apply Fix",
    "get_connected_repos": "GitHub Repos",
    "jenkins_rca": "Jenkins RCA",
    "spinnaker_rca": "Spinnaker RCA",
    "cloudbees_rca": "CloudBees RCA",
    "tailscale_ssh": "Tailscale SSH",
    "analyze_zip_file": "ZIP Analyzer",
    "rag_index_zip": "RAG Indexer",
    "knowledge_base_search": "Knowledge Base",
    "save_discovery_finding": "Discovery Finding",
    "search_splunk": "Splunk Search",
    "list_splunk_indexes": "Splunk Indexes",
    "list_splunk_sourcetypes": "Splunk Sourcetypes",
    "query_dynatrace": "Dynatrace",
    "query_datadog": "Datadog",
    "query_newrelic": "New Relic",
    "query_opsgenie": "OpsGenie",
    "list_incidentio_incidents": "incident.io",
    "get_incidentio_incident": "incident.io",
    "get_incidentio_timeline": "incident.io Timeline",
    "confluence_runbook_parse": "Confluence Runbook",
    "confluence_search_similar": "Confluence Search",
    "confluence_search_runbooks": "Confluence Runbooks",
    "confluence_fetch_page": "Confluence Page",
    "sharepoint_search": "SharePoint Search",
    "sharepoint_fetch_page": "SharePoint Page",
    "sharepoint_fetch_document": "SharePoint Document",
    "sharepoint_create_page": "SharePoint: Create Page",
    "jira_search_issues": "Jira Search",
    "jira_get_issue": "Jira Issue",
    "jira_add_comment": "Jira: Add Comment",
    "jira_create_issue": "Jira: Create Issue",
    "jira_update_issue": "Jira: Update Issue",
    "jira_link_issues": "Jira: Link Issues",
    "bitbucket_repos": "Bitbucket Repos",
    "bitbucket_branches": "Bitbucket Branches",
    "bitbucket_pull_requests": "Bitbucket PRs",
    "bitbucket_issues": "Bitbucket Issues",
    "bitbucket_pipelines": "Bitbucket Pipelines",
    "query_cloudflare": "Cloudflare",
    "cloudflare_list_zones": "Cloudflare Zones",
    "cloudflare_action": "Cloudflare Action",
    "thousandeyes_list_tests": "ThousandEyes Tests",
    "thousandeyes_get_test_detail": "ThousandEyes Test Detail",
    "thousandeyes_get_test_results": "ThousandEyes Test Results",
    "thousandeyes_get_alerts": "ThousandEyes Alerts",
    "thousandeyes_get_alert_rules": "ThousandEyes Alert Rules",
    "thousandeyes_get_agents": "ThousandEyes Agents",
    "thousandeyes_get_endpoint_agents": "ThousandEyes Endpoint Agents",
    "thousandeyes_get_internet_insights": "ThousandEyes Internet Insights",
    "thousandeyes_get_dashboards": "ThousandEyes Dashboards",
    "thousandeyes_get_dashboard_widget": "ThousandEyes Dashboard Widget",
    "thousandeyes_get_bgp_monitors": "ThousandEyes BGP Monitors",
    "notion_export_postmortem": "Notion: Export Postmortem",
    "notion_update_database_properties": "Notion: Update DB",
    "web_search": "Web Search",
    "coroot_get_incidents": "Coroot Incidents",
    "coroot_get_incident_detail": "Coroot Incident Detail",
    "coroot_get_applications": "Coroot Applications",
    "coroot_get_app_detail": "Coroot App Detail",
    "coroot_get_app_logs": "Coroot App Logs",
    "coroot_get_traces": "Coroot Traces",
    "coroot_get_service_map": "Coroot Service Map",
    "coroot_query_metrics": "Coroot Metrics",
    "coroot_get_deployments": "Coroot Deployments",
    "coroot_get_nodes": "Coroot Nodes",
    "coroot_get_overview_logs": "Coroot Overview Logs",
    "coroot_get_node_detail": "Coroot Node Detail",
    "coroot_get_costs": "Coroot Costs",
    "coroot_get_risks": "Coroot Risks",
}

# Internal coordination tools that show up as tool messages but aren't real
# evidence — filter them out of citations entirely.
_NON_EVIDENCE_TOOLS = frozenset({
    "dispatch_subagent",
    "write_findings",
    "load_skill",
    "trigger_rca",
})

# Keys that mark a tool response as carrying real data (not just a status
# message). Used to gate the "message" fallback in _extract_output.
_OUTPUT_DATA_KEYS = frozenset({
    "incidents", "applications", "entries", "services",
    "failing_checks", "app_id", "total_incidents",
    "total_applications", "total_entries", "total_services",
})


@dataclass
class Citation:
    """Represents a single piece of evidence from an RCA investigation."""
    index: int
    tool_name: str
    command: str
    output: str
    timestamp: Optional[datetime]
    tool_call_id: str


class CitationExtractor:
    """Extracts tool call citations from chat session history."""

    def extract_citations_from_session(
        self,
        session_id: str,
        user_id: str,
        incident_id: Optional[str] = None,
    ) -> List[Citation]:
        """Extract all tool calls and outputs from a chat session.

        When ``incident_id`` is provided, ``dispatch_subagent`` citations are
        expanded inline into the underlying sub-agent tool calls (one citation
        per real tool call) so the downstream summary LLM cites individual
        evidence rows the same way it does in single-agent mode.
        """
        from utils.db.connection_pool import db_pool
        from utils.auth.stateless_auth import set_rls_context

        try:
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cursor:
                    set_rls_context(cursor, conn, user_id, log_prefix="[CitationExtractor:extract_citations_from_session]")
                    cursor.execute(
                        """
                        SELECT llm_context_history
                        FROM chat_sessions
                        WHERE id = %s AND user_id = %s
                        """,
                        (session_id, user_id),
                    )
                    row = cursor.fetchone()
                    if not row or row[0] is None:
                        logger.warning(
                            f"[CitationExtractor] No llm_context_history found for session {session_id}"
                        )
                        return []

                    llm_context = row[0]
                    if isinstance(llm_context, str):
                        try:
                            llm_context = json.loads(llm_context)
                        except json.JSONDecodeError:
                            logger.error(
                                f"[CitationExtractor] Failed to parse llm_context_history for session {session_id}"
                            )
                            return []

                    sub_agent_history = (
                        self._load_sub_agent_history(cursor, incident_id) if incident_id else {}
                    )
            return self._parse_tool_messages(llm_context, sub_agent_history)

        except Exception as e:
            logger.exception(
                f"[CitationExtractor] Failed to extract citations for session {session_id}: {e}"
            )
            return []

    def _load_sub_agent_history(
        self, cursor, incident_id: str
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Load tool_call_history per agent_id for the given incident.

        Reuses the caller's cursor (and thus its RLS context) so we don't
        check out a second connection or set the RLS GUC twice.
        """
        history: Dict[str, List[Dict[str, Any]]] = {}
        try:
            cursor.execute(
                """
                SELECT agent_id, tool_call_history
                FROM rca_findings
                WHERE incident_id = %s
                """,
                (incident_id,),
            )
            for agent_id, raw in cursor.fetchall():
                if not raw:
                    continue
                if isinstance(raw, str):
                    try:
                        raw = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                if isinstance(raw, list):
                    history[agent_id] = raw
        except Exception:
            logger.exception(
                f"[CitationExtractor] Failed to load sub-agent history for incident {incident_id}"
            )
        return history

    def _parse_tool_messages(
        self,
        messages: List[Dict[str, Any]],
        sub_agent_history: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    ) -> List[Citation]:
        """
        Parse messages to extract tool call information.

        Args:
            messages: List of serialized messages from llm_context_history
            sub_agent_history: Map of agent_id -> tool_call_history rows. When
                provided, `dispatch_subagent` tool messages are expanded into
                one citation per underlying real tool call.

        Returns:
            List of Citation objects
        """
        citations: List[Citation] = []
        tool_call_map: Dict[str, Dict[str, Any]] = {}
        sub_agent_history = sub_agent_history or {}
        # Tracks (agent_id, underlying tool_call_id) we've already emitted so
        # retried / re-dispatched sub-agents don't double-cite.
        emitted_inner: set = set()

        for msg in messages:
            if msg.get("role") == "ai" and msg.get("tool_calls"):
                for tool_call in msg.get("tool_calls", []):
                    tool_id = tool_call.get("id")
                    if tool_id:
                        tool_call_map[tool_id] = {
                            "name": tool_call.get("name", "unknown"),
                            "args": tool_call.get("args", {}),
                            "timestamp": msg.get("timestamp"),
                        }

        index = 1
        for msg in messages:
            if msg.get("role") != "tool":
                continue
            tool_call_id = msg.get("tool_call_id") or msg.get("id")
            content = msg.get("content", "")
            tool_name = msg.get("name", "")
            timestamp_str = msg.get("timestamp")

            call_info = tool_call_map.get(tool_call_id, {})
            if not tool_name:
                tool_name = call_info.get("name", "unknown")

            if tool_name == DISPATCH_SUBAGENT_TOOL_NAME:
                inner_rows = self._iter_subagent_evidence(
                    call_info.get("args", {}), sub_agent_history, emitted_inner,
                )
                for row in inner_rows:
                    citations.append(Citation(index=index, **row))
                    index += 1
                # dispatch_subagent itself is internal coordination — never let
                # the dispatch tool message land as its own citation, even if
                # expansion produced no rows.
                continue

            if tool_name in _NON_EVIDENCE_TOOLS:
                continue

            command = self._extract_command(call_info.get("args", {}), content, tool_name)
            output = self._extract_output(content)
            if not output or output.strip() == "":
                continue

            ts_source = timestamp_str or call_info.get("timestamp")
            timestamp = self._parse_timestamp(ts_source) if ts_source else None

            citations.append(
                Citation(
                    index=index,
                    tool_name=self._normalize_tool_name(tool_name),
                    command=command,
                    output=output,
                    timestamp=timestamp,
                    tool_call_id=tool_call_id or "",
                )
            )
            index += 1

        logger.info(
            f"[CitationExtractor] Extracted {len(citations)} citations from chat session"
        )
        return citations

    def _iter_subagent_evidence(
        self,
        dispatch_args: Dict[str, Any],
        sub_agent_history: Dict[str, List[Dict[str, Any]]],
        emitted: set,
    ) -> List[Dict[str, Any]]:
        """Return the per-tool-call Citation kwargs for one dispatch_subagent
        call, deduped against ``emitted`` (which is mutated to record what was
        consumed). Caller assigns ``index``."""
        agent_id = dispatch_args.get("agent_id") if dispatch_args else None
        if not agent_id:
            return []
        rows: List[Dict[str, Any]] = []
        for entry in sub_agent_history.get(agent_id) or []:
            if not isinstance(entry, dict):
                continue
            inner_id = entry.get("tool_call_id") or ""
            dedupe_key = (agent_id, inner_id) if inner_id else None
            if dedupe_key and dedupe_key in emitted:
                continue
            output_excerpt = entry.get("output_excerpt") or ""
            if not output_excerpt.strip():
                continue
            inner_tool = entry.get("tool_name") or "unknown"
            if inner_tool in _NON_EVIDENCE_TOOLS:
                continue
            # `command` is captured server-side before args truncation; prefer
            # it. Fall back to parsing args/output for entries without it.
            command = entry.get("command") or ""
            if not command:
                args = self._parse_history_args(entry.get("args") or entry.get("input"))
                command = self._extract_command(args, output_excerpt, inner_tool)
            started_at = entry.get("started_at")
            rows.append({
                "tool_name": self._normalize_tool_name(inner_tool),
                "command": command,
                "output": self._extract_output(output_excerpt),
                "timestamp": self._parse_timestamp(started_at) if started_at else None,
                "tool_call_id": inner_id,
            })
            if dedupe_key:
                emitted.add(dedupe_key)
        return rows

    @staticmethod
    def _parse_history_args(raw: Any) -> Dict[str, Any]:
        """Decode the JSON-encoded ``args`` field stored by sub_agent.py
        ``_serialize_args``. Returns ``{}`` for anything that isn't a JSON
        object so callers can use ``.get(...)`` unconditionally."""
        if isinstance(raw, dict):
            return raw
        if not isinstance(raw, str) or not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _extract_command(self, args: Dict[str, Any], content: str, tool_name: str = "") -> str:
        """Extract the command from tool args or parsed output."""
        # Try to get command from args first
        if args:
            if "command" in args:
                return str(args["command"])
            if "query" in args:
                return str(args["query"])
            if "path" in args:
                return str(args["path"])
            if "promql" in args:
                return str(args["promql"])

            if tool_name.startswith("coroot_"):
                coroot_args = {k: v for k, v in args.items()
                              if k not in ("user_id", "session_id", "project_id") and v is not None}
                if coroot_args:
                    parts = [f"{k}={v}" for k, v in coroot_args.items()]
                    candidate = ", ".join(parts)
                    if candidate:
                        return candidate

        # Try to parse from JSON content
        try:
            parsed = json.loads(content) if isinstance(content, str) else content
            if isinstance(parsed, dict):
                if "final_command" in parsed:
                    return str(parsed["final_command"])
                if "command" in parsed:
                    return str(parsed["command"])
                if "query" in parsed:
                    return str(parsed["query"])
        except (json.JSONDecodeError, TypeError):
            pass

        return "Command not available"

    def _extract_output(self, content: str) -> str:
        """Extract the meaningful output from tool response."""
        if not content:
            return ""

        # Try to parse as JSON
        try:
            parsed = json.loads(content) if isinstance(content, str) else content
            if isinstance(parsed, dict):
                # Common output field names - check chat_output first (used by cloud tools)
                for field in ["chat_output", "output", "result", "data", "response"]:
                    if field in parsed and parsed[field]:
                        return str(parsed[field])

                # "message" alone is often a status string (e.g. "No incidents in the last 24h").
                # Only use it when no richer data fields are present (Coroot responses tend to
                # carry both "message" and "incidents"/"applications"/etc.).
                if "message" in parsed and parsed["message"] and not _OUTPUT_DATA_KEYS.intersection(parsed.keys()):
                    return str(parsed["message"])

                # If no specific output field, return the whole structure for RenderOutput to handle
                return json.dumps(parsed, indent=2)
        except (json.JSONDecodeError, TypeError):
            pass

        # Return raw content if not JSON
        return str(content)

    def _normalize_tool_name(self, tool_name: str) -> str:
        """Normalize tool name for display."""
        if tool_name.startswith("mcp_"):
            actual_name = tool_name[4:].replace("_", " ").title()
            return f"MCP: {actual_name}"
        return _TOOL_NAME_MAPPING.get(tool_name, tool_name)

    def _parse_timestamp(self, ts: Any) -> Optional[datetime]:
        """Parse timestamp from various formats."""
        if isinstance(ts, datetime):
            return ts
        if isinstance(ts, (int, float)):
            try:
                return datetime.fromtimestamp(ts)
            except (ValueError, OSError):
                return None
        if isinstance(ts, str):
            # Try ISO format
            for fmt in [
                "%Y-%m-%dT%H:%M:%S.%f",
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S",
            ]:
                try:
                    return datetime.strptime(ts.replace("Z", ""), fmt)
                except ValueError:
                    continue
        return None


def save_incident_citations(
    incident_id: str, citations: List[Citation]
) -> None:
    """
    Save citations to the incident_citations table.

    Args:
        incident_id: The incident UUID
        citations: List of Citation objects to save
    """
    from utils.db.connection_pool import db_pool

    if not citations:
        logger.info(f"[CitationExtractor] No citations to save for incident {incident_id}")
        return

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                # No RLS needed — incident_citations not RLS-protected
                cursor.execute(
                    "DELETE FROM incident_citations WHERE incident_id = %s",
                    (incident_id,)
                )

                # Insert new citations
                for citation in citations:
                    cursor.execute(
                        """
                        INSERT INTO incident_citations
                        (incident_id, citation_key, tool_name, command, output, executed_at)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (incident_id, citation_key) DO UPDATE SET
                            tool_name = EXCLUDED.tool_name,
                            command = EXCLUDED.command,
                            output = EXCLUDED.output,
                            executed_at = EXCLUDED.executed_at
                        """,
                        (
                            incident_id,
                            str(citation.index),
                            citation.tool_name,
                            citation.command,
                            citation.output,
                            citation.timestamp,
                        ),
                    )
                conn.commit()

        logger.info(
            f"[CitationExtractor] Saved {len(citations)} citations for incident {incident_id}"
        )

    except Exception as e:
        logger.exception(
            f"[CitationExtractor] Failed to save citations for incident {incident_id}: {e}"
        )
