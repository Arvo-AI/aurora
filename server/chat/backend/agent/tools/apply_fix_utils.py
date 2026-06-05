"""Shared utilities for apply-fix tools (GitHub, GitLab, Bitbucket)."""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from utils.db.connection_pool import db_pool
from utils.auth.stateless_auth import set_rls_context

logger = logging.getLogger(__name__)


def generate_branch_name(incident_id: str) -> str:
    """Generate a unique branch name for a fix."""
    incident_short = incident_id[:8] if incident_id else "unknown"
    timestamp = int(time.time())
    return f"fix/aurora-{incident_short}-{timestamp}"


def get_fix_suggestion(suggestion_id: int, user_id: str) -> Optional[dict]:
    """Fetch a fix suggestion from the database."""
    try:
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()
            set_rls_context(cursor, conn, user_id, log_prefix="[ApplyFix:get_fix_suggestion]")
            cursor.execute(
                """SELECT s.id, s.incident_id, s.title, s.description, s.type,
                          s.file_path, s.original_content, s.suggested_content,
                          s.user_edited_content, s.repository, s.command,
                          s.pr_url, s.created_branch
                   FROM incident_suggestions s
                   JOIN incidents i ON s.incident_id = i.id
                   WHERE s.id = %s AND s.type = 'fix'""",
                (suggestion_id,),
            )
            row = cursor.fetchone()
            if row:
                return {
                    "id": row[0],
                    "incident_id": str(row[1]),
                    "title": row[2],
                    "description": row[3],
                    "type": row[4],
                    "file_path": row[5],
                    "original_content": row[6],
                    "suggested_content": row[7],
                    "user_edited_content": row[8],
                    "repository": row[9],
                    "commit_message": row[10],
                    "pr_url": row[11],
                    "created_branch": row[12],
                }
    except Exception:
        logger.exception("Failed to fetch fix suggestion %s", suggestion_id)
    return None


def build_pr_body(suggestion: dict, file_path: str) -> str:
    """Build the PR/MR description body with incident context."""
    return (
        f"## Incident Fix\n\n"
        f"**Incident ID**: {suggestion.get('incident_id', 'N/A')}\n\n"
        f"### Description\n{suggestion.get('description', 'No description')}\n\n"
        f"### File Changed\n- `{file_path}`\n\n"
        f"---\n*This PR was created by Aurora from an RCA fix suggestion.*\n"
    )


def update_suggestion_with_pr(
    suggestion_id: int,
    pr_url: str,
    pr_number: int,
    created_branch: str,
) -> bool:
    """Update an incident_suggestions row with PR metadata after successful creation."""
    try:
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """UPDATE incident_suggestions
                   SET pr_url = %s, pr_number = %s, created_branch = %s, applied_at = %s
                   WHERE id = %s""",
                (pr_url, pr_number, created_branch, datetime.now(timezone.utc), suggestion_id),
            )
            conn.commit()
            return True
    except Exception:
        logger.exception("Failed to update suggestion %s with PR info", suggestion_id)
        return False
