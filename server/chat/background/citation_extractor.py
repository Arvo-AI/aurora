"""Extract citations from RCA chat sessions for evidence linking."""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Dict, Any

logger = logging.getLogger(__name__)


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
        self, session_id: str, user_id: str
    ) -> List[Citation]:
        """
        Extract all tool calls and outputs from a chat session.

        Args:
            session_id: The chat session ID
            user_id: The user ID

        Returns:
            List of Citation objects with sequential indices
        """
        from utils.db.connection_pool import db_pool

        try:
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cursor:
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

                    return self._parse_tool_messages(llm_context)

        except Exception as e:
            logger.exception(
                f"[CitationExtractor] Failed to extract citations for session {session_id}: {e}"
            )
            return []

    def _parse_tool_messages(self, messages: List[Dict[str, Any]]) -> List[Citation]:
        """
        Parse messages to extract tool call information.

        Args:
            messages: List of serialized messages from llm_context_history

        Returns:
            List of Citation objects
        """
        citations = []
        tool_call_map: Dict[str, Dict[str, Any]] = {}

        # First pass: collect tool call info from AIMessages
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

        # Second pass: match tool results to tool calls
        index = 1
        for msg in messages:
            if msg.get("role") == "tool":
                tool_call_id = msg.get("tool_call_id") or msg.get("id")
                content = msg.get("content", "")
                tool_name = msg.get("name", "")
                timestamp_str = msg.get("timestamp")

                # Get additional info from the tool call map
                call_info = tool_call_map.get(tool_call_id, {})
                if not tool_name:
                    tool_name = call_info.get("name", "unknown")

                # Parse the command from tool args or output
                command = self._extract_command(call_info.get("args", {}), content)

                # Parse output
                output = self._extract_output(content)

                # Skip empty outputs
                if not output or output.strip() == "":
                    continue

                # Parse timestamp
                timestamp = None
                ts_source = timestamp_str or call_info.get("timestamp")
                if ts_source:
                    timestamp = self._parse_timestamp(ts_source)

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

    def _extract_command(self, args: Dict[str, Any], content: str) -> str:
        """Extract the command from tool args or parsed output."""
        # Try to get command from args first
        if args:
            if "command" in args:
                return str(args["command"])
            if "query" in args:
                return str(args["query"])
            if "path" in args:
                return str(args["path"])

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
                for field in ["chat_output", "output", "message", "result", "data", "response"]:
                    if field in parsed and parsed[field]:
                        return str(parsed[field])

                # If no specific output field, return the whole structure for RenderOutput to handle
                return json.dumps(parsed, indent=2)
        except (json.JSONDecodeError, TypeError):
            pass

        # Return raw content if not JSON
        return str(content)

    def _normalize_tool_name(self, tool_name: str) -> str:
        """Normalize tool name for display."""
        name_mapping = {
            "bash": "Terminal",
            "run_bash_command": "Terminal",
            "terminal_exec": "Terminal",
            "read_file": "File Read",
            "write_file": "File Write",
            "search_code": "Code Search",
            "execute_kubectl": "kubectl",
            "on_prem_kubectl": "kubectl",
            "gcloud_command": "gcloud",
            "aws_command": "AWS CLI",
            "azure_command": "Azure CLI",
            "cloud_exec": "Cloud CLI",  # Unified cloud command tool (gcloud/aws/azure)
            "iac_tool": "Terraform",
            "github_commit": "GitHub",
            "tailscale_ssh": "Tailscale SSH",
            "analyze_zip_file": "ZIP Analyzer",
            "rag_index_zip": "RAG Indexer",
            "search_splunk": "Splunk Search",
            "list_splunk_indexes": "Splunk Indexes",
            "list_splunk_sourcetypes": "Splunk Sourcetypes",
            "query_dynatrace": "Dynatrace",
            "web_search": "Web Search",
        }
        # Handle MCP tools (prefixed with mcp_)
        if tool_name.startswith("mcp_"):
            # Extract the actual tool name and capitalize
            actual_name = tool_name[4:].replace("_", " ").title()
            return f"MCP: {actual_name}"
        
        return name_mapping.get(tool_name, tool_name)

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
                # Delete existing citations for this incident
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
