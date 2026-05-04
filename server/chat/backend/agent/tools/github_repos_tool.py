"""
Agent tool: get_connected_repos
Returns all GitHub repos the user has connected, with metadata summaries.
The agent uses this to decide which repo(s) to investigate during RCA.
"""
import json
import logging
from pydantic import BaseModel

from utils.auth.github_auth_router import (
    NoGitHubAuthError,
    get_any_auth_for_user,
)

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
        auth = get_any_auth_for_user(user_id)
    except NoGitHubAuthError:
        return "GitHub not connected for this user. Install the GitHub App or connect via OAuth."
    except Exception as e:
        logger.error(f"Error resolving GitHub auth for user {user_id}: {e}", exc_info=True)
        return json.dumps({"error": f"Failed to resolve GitHub auth: {e}"})

    try:
        from utils.db.connection_pool import db_pool
        from utils.auth.stateless_auth import set_rls_context
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[GithubRepos:list]")
                cur.execute(
                    """SELECT repo_full_name, default_branch, is_private, metadata_summary, metadata_status
                       FROM github_connected_repos
                       WHERE user_id = %s
                       ORDER BY repo_full_name""",
                    (user_id,),
                )
                rows = cur.fetchall()

        if not rows:
            return json.dumps({
                "repos": [],
                "auth_method": auth.method,
                "installation_id": auth.installation_id,
                "message": "No GitHub repos connected. Ask the user to connect repos in Settings > Connectors > GitHub.",
            })

        repos = [
            {
                "repo": r[0],
                "branch": r[1] or "main",
                "private": r[2],
                "description": r[3] or ("(description generating...)" if r[4] != 'ready' else "(no description)"),
            }
            for r in rows
        ]
        return json.dumps({
            "repos": repos,
            "auth_method": auth.method,
            "installation_id": auth.installation_id,
        })
    except Exception as e:
        logger.error(f"Error fetching connected repos: {e}", exc_info=True)
        return json.dumps({"error": f"Failed to fetch connected repos: {e}"})
