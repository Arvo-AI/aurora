"""
Agent tool: get_connected_repos
Returns all GitHub repos the user has connected, with metadata summaries.
The agent uses this to decide which repo(s) to investigate during RCA.
"""
import json
import logging
from typing import Optional
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class GetConnectedReposArgs(BaseModel):
    """No required args -- reads from user context."""
    pass


def get_connected_repos(**kwargs) -> str:
    """Return connected GitHub repositories with their descriptions."""
    user_id = kwargs.get("user_id")
    if not user_id:
        return json.dumps({"error": "No user context available"})

    try:
        from utils.db.connection_pool import db_pool
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT repo_full_name, default_branch, is_private, metadata_summary
                       FROM github_connected_repos
                       WHERE user_id = %s AND metadata_status = 'ready'
                       ORDER BY repo_full_name""",
                    (user_id,),
                )
                rows = cur.fetchall()

        if not rows:
            return json.dumps({"repos": [], "message": "No GitHub repos connected. Ask the user to connect repos in Settings > Connectors > GitHub."})

        repos = [
            {
                "repo": r[0],
                "branch": r[1] or "main",
                "private": r[2],
                "description": r[3] or "(no description yet)",
            }
            for r in rows
        ]
        return json.dumps({"repos": repos})
    except Exception as e:
        logger.error(f"Error fetching connected repos: {e}", exc_info=True)
        return json.dumps({"error": f"Failed to fetch connected repos: {e}"})
