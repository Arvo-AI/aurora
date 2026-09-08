"""Extract and save structured suggestions from RCA investigations."""

import logging
import re
from dataclasses import dataclass
from typing import List, Optional

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
    r"--all(?!-)\b",
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
    type: str  # 'diagnostic', 'mitigation', 'remediate', 'prevent', 'fix'
    risk: str  # 'safe', 'low', 'medium', 'high'
    command: Optional[str]
    rationale: Optional[str] = None
    undo: Optional[str] = None
    file_path: Optional[str] = None
    summary: Optional[str] = None


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
                # No RLS needed — incident_suggestions not RLS-protected
                cursor.execute(
                    "DELETE FROM incident_suggestions WHERE incident_id = %s AND type != 'fix'",
                    (incident_id,),
                )

                # Batch insert new suggestions
                cursor.executemany(
                    """
                    INSERT INTO incident_suggestions
                    (incident_id, title, description, type, risk, command, rationale, undo, file_path, summary)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            incident_id,
                            s.title,
                            s.description,
                            s.type,
                            s.risk,
                            s.command,
                            getattr(s, "rationale", None),
                            getattr(s, "undo", None),
                            getattr(s, "file_path", None),
                            getattr(s, "summary", None),
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
