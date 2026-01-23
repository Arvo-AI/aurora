"""Extract and save structured suggestions from RCA investigations."""

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import List, Optional, Any

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

from chat.background.citation_extractor import Citation

logger = logging.getLogger(__name__)

# Dangerous command patterns that should be blocked
DANGEROUS_PATTERNS = [
    # Destructive operations
    r"\bdelete\b",
    r"\bremove\b",
    r"\bdestroy\b",
    r"\bterminate\b",
    r"\bdrop\b",
    r"\btruncate\b",
    r"\bpurge\b",
    r"\bkill\b",
    # Dangerous flags
    r"--force",
    r"--hard",
    r"--all\b",
    r"-rf\b",
    r"--no-preserve-root",
    # Shell injection risks
    r"\brm\s+-rf",
    r"\brm\s+-r",
    r">\s*/dev/",
    r"\|.*rm\b",
    r";\s*rm\b",
    r"&&\s*rm\b",
    # Database destructive
    r"\bDROP\b",
    r"\bTRUNCATE\b",
    r"\bDELETE\s+FROM\b",
    # Cloud destructive
    r"instances\s+delete",
    r"clusters\s+delete",
    r"deployments\s+delete",
    r"services\s+delete",
    r"volumes\s+delete",
]

# Compiled regex for performance
DANGEROUS_REGEX = re.compile("|".join(DANGEROUS_PATTERNS), re.IGNORECASE)

# Max command length to prevent injection attempts
MAX_COMMAND_LENGTH = 500


def is_command_safe(command: Optional[str]) -> bool:
    """Check if a command is safe to store and potentially execute."""
    if not command:
        return True  # Empty command is safe
    if len(command) > MAX_COMMAND_LENGTH:
        return False
    return not DANGEROUS_REGEX.search(command)


@dataclass
class Suggestion:
    """Represents an actionable suggestion with an optional command."""

    title: str
    description: str
    type: str  # 'diagnostic', 'mitigation', 'communication'
    risk: str  # 'safe', 'low', 'medium', 'high'
    command: Optional[str]


class SuggestionExtractor:
    """Extract structured suggestions from RCA chat context."""

    def extract_suggestions(
        self,
        incident_id: str,
        summary: str,
        citations: List[Citation],
        service: str,
        alert_title: str,
    ) -> List[Suggestion]:
        """
        Use LLM to generate structured suggestions with commands.

        Args:
            incident_id: The incident UUID
            summary: The generated incident summary
            citations: List of Citation objects from the RCA
            service: The affected service name
            alert_title: The alert title

        Returns:
            List of Suggestion objects
        """
        citation_context = self._build_citation_context(citations)

        prompt = self._build_suggestions_prompt(
            summary=summary,
            citation_context=citation_context,
            service=service,
            alert_title=alert_title,
        )

        try:
            from chat.backend.agent.providers import create_chat_model

            llm = create_chat_model(
                "google/gemini-3-pro-preview",
                temperature=0.3,
            )

            response = llm.invoke([HumanMessage(content=prompt)])
            suggestions = self._parse_suggestions_response(response.content)

            logger.info(
                f"[SuggestionExtractor] Generated {len(suggestions)} suggestions for incident {incident_id}"
            )
            return suggestions

        except Exception as e:
            logger.exception(
                f"[SuggestionExtractor] Failed to extract suggestions for incident {incident_id}: {e}"
            )
            return []

    def _build_citation_context(self, citations: List[Citation]) -> str:
        """Build context string from citations."""
        if not citations:
            return "No investigation evidence available."

        lines = []
        # Use last 15 citations for context (most recent/relevant)
        for c in citations[-15:]:
            tool_name = c.tool_name
            command = c.command
            # Truncate output to avoid context overflow
            output = c.output[:500] if c.output else "No output"
            lines.append(f"- {tool_name}: {command}\n  Output snippet: {output}")

        return "\n".join(lines)

    def _build_suggestions_prompt(
        self,
        summary: str,
        citation_context: str,
        service: str,
        alert_title: str,
    ) -> str:
        """Build the prompt for generating structured suggestions."""
        return f"""Based on this incident analysis, generate 2-4 actionable suggestions with executable commands.

INCIDENT:
- Service: {service}
- Alert: {alert_title}

SUMMARY:
{summary}

INVESTIGATION EVIDENCE (tools and commands that were used):
{citation_context}

Generate suggestions as a JSON array. Each suggestion MUST have:
- "title": Short action title (e.g., "Check pod logs", "Restart deployment")
- "description": 1-2 sentence explanation of what this will help diagnose or fix
- "type": One of "diagnostic" (read-only investigation), "mitigation" (fix the issue), or "communication" (notify stakeholders)
- "risk": One of "safe" (read-only), "low" (minor changes), "medium" (service restart), "high" (data loss risk)
- "command": The exact CLI command to run (kubectl, gcloud, aws, az, splunk search, etc.)

GUIDELINES:
- For diagnostic suggestions, use read-only commands (get, describe, logs, list, search)
- For mitigation suggestions, include the actual remediation command but mark appropriately by risk
- Commands should be complete and executable (include namespaces, resource names from the investigation)
- Reference specific resources mentioned in the investigation evidence
- Prefer kubectl, gcloud, aws, az commands based on what tools were used in the investigation

Return ONLY a valid JSON array, no markdown formatting, no explanation:
[
  {{"title": "...", "description": "...", "type": "diagnostic", "risk": "safe", "command": "kubectl logs ..."}},
  {{"title": "...", "description": "...", "type": "mitigation", "risk": "medium", "command": "kubectl rollout restart ..."}}
]"""

    def _parse_suggestions_response(self, content: Any) -> List[Suggestion]:
        """Parse LLM response into Suggestion objects."""
        if not content:
            return []

        # Handle Gemini thinking model responses (list with thinking/text blocks)
        if isinstance(content, list):
            text_parts = []
            for part in content:
                if isinstance(part, dict):
                    part_type = part.get("type", "")
                    # Filter out thinking/reasoning blocks
                    if part_type not in ("thinking", "reasoning"):
                        text = part.get("text", "")
                        if text:
                            text_parts.append(str(text))
                elif isinstance(part, str):
                    text_parts.append(part)
            text = "".join(text_parts).strip()
        else:
            text = str(content).strip()

        # Remove markdown code blocks if present
        if text.startswith("```"):
            lines = text.split("\n")
            end_index = -1 if lines[-1] == "```" else len(lines)
            text = "\n".join(lines[1:end_index]).strip()

        try:
            suggestions_data = json.loads(text)
        except json.JSONDecodeError as e:
            # Try to fix common escape sequence issues from LLM output
            # LLMs often produce invalid escapes like \n in strings instead of \\n
            logger.warning(
                f"[SuggestionExtractor] Initial JSON parse failed: {e}. Attempting to fix escapes."
            )
            try:
                # Fix invalid escape sequences by escaping lone backslashes
                # Match backslash followed by a character that's not a valid JSON escape
                # Valid JSON escapes: \", \\, \/, \b, \f, \n, \r, \t, \uXXXX
                fixed_text = re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", text)
                suggestions_data = json.loads(fixed_text)
                logger.info(
                    "[SuggestionExtractor] Successfully parsed JSON after fixing escapes"
                )
            except json.JSONDecodeError as e2:
                logger.error(
                    f"[SuggestionExtractor] Failed to parse JSON response: {e2}. Content: {text[:200]}"
                )
                return []

        if not isinstance(suggestions_data, list):
            logger.warning("[SuggestionExtractor] Response is not a list, wrapping")
            suggestions_data = [suggestions_data]

        suggestions = []
        for item in suggestions_data:
            if not isinstance(item, dict):
                continue

            title = item.get("title", "").strip()
            if not title:
                continue

            command = item.get("command")

            # Filter out dangerous commands
            if command and not is_command_safe(command):
                logger.warning(
                    f"[SuggestionExtractor] Blocked dangerous command: {command[:100]}"
                )
                continue

            suggestions.append(
                Suggestion(
                    title=title,
                    description=item.get("description", "").strip(),
                    type=item.get("type", "diagnostic"),
                    risk=item.get("risk", "safe"),
                    command=command,
                )
            )

        return suggestions


def save_incident_suggestions(incident_id: str, suggestions: List[Suggestion]) -> None:
    """
    Save suggestions to the incident_suggestions table.

    Args:
        incident_id: The incident UUID
        suggestions: List of Suggestion objects to save
    """
    from utils.db.connection_pool import db_pool

    if not suggestions:
        logger.info(
            f"[SuggestionExtractor] No suggestions to save for incident {incident_id}"
        )
        return

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                # Delete existing suggestions for this incident
                cursor.execute(
                    "DELETE FROM incident_suggestions WHERE incident_id = %s",
                    (incident_id,),
                )

                # Batch insert new suggestions
                cursor.executemany(
                    """
                    INSERT INTO incident_suggestions
                    (incident_id, title, description, type, risk, command)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            incident_id,
                            s.title,
                            s.description,
                            s.type,
                            s.risk,
                            s.command,
                        )
                        for s in suggestions
                    ],
                )
                conn.commit()

        logger.info(
            f"[SuggestionExtractor] Saved {len(suggestions)} suggestions for incident {incident_id}"
        )

    except Exception as e:
        logger.exception(
            f"[SuggestionExtractor] Failed to save suggestions for incident {incident_id}: {e}"
        )
