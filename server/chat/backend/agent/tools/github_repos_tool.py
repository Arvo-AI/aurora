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
from utils.auth.github_auth_mode import is_oauth_token_honored
from utils.auth.token_management import get_token_data

logger = logging.getLogger(__name__)


class GetConnectedReposArgs(BaseModel):
    """No required args -- reads from user context."""
    pass


def _user_has_oauth(user_id: str) -> bool:
    if not is_oauth_token_honored():
        return False
    creds = get_token_data(user_id, "github")
    return bool(creds and creds.get("access_token"))


def get_connected_repos(**kwargs) -> str:
    """Return connected GitHub repositories with their descriptions."""
    user_id = kwargs.get("user_id")
    if not user_id:
        return json.dumps({"error": "No user context available"})

    try:
        get_any_auth_for_user(user_id)
    except NoGitHubAuthError:
        return json.dumps({
            "error": "GitHub not connected for this user. Install the GitHub App or connect via OAuth.",
        })
    except Exception as e:
        logger.exception("Error resolving GitHub auth for user %s", user_id)
        return json.dumps({"error": f"Failed to resolve GitHub auth: {e}"})

    try:
        user_has_oauth = _user_has_oauth(user_id)
    except Exception as e:
        logger.exception("Error checking OAuth status for user %s", user_id)
        return json.dumps({"error": f"Failed to read GitHub credentials: {e}"})

    try:
        from utils.db.connection_pool import db_pool
        from utils.auth.stateless_auth import set_rls_context
        from utils.db.org_scope import resolve_org, org_read_predicate
        org_id = resolve_org(user_id)
        predicate, pred_params = org_read_predicate(user_id, org_id)
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[GithubRepos:list]")
                cur.execute(
                    f"""SELECT DISTINCT ON (r.repo_full_name)
                              r.repo_full_name, r.default_branch, r.is_private,
                              r.metadata_summary, r.metadata_status,
                              r.installation_id,
                              (i.installation_id IS NOT NULL
                                  AND i.suspended_at IS NULL) AS has_active_installation
                       FROM (
                           SELECT *
                             FROM connected_repos
                            WHERE provider = 'github'
                              AND {predicate}
                       ) r
                       LEFT JOIN github_installations i
                              ON i.installation_id = r.installation_id
                       ORDER BY r.repo_full_name, r.updated_at DESC""",
                    pred_params,
                )
                rows = cur.fetchall()

        repos = []
        for r in rows:
            installation_id, has_active = r[5], r[6]
            if installation_id is not None and has_active:
                auth_method = "app"
            elif user_has_oauth:
                auth_method = "oauth"
            else:
                continue
            repos.append({
                "repo": r[0],
                "branch": r[1] or "main",
                "private": r[2],
                "description": r[3] or ("(description generating...)" if r[4] != 'ready' else "(no description)"),
                "installation_id": installation_id if auth_method == "app" else None,
                "auth_method": auth_method,
            })

        if not repos:
            return json.dumps({
                "repos": [],
                "message": "No GitHub repos connected with usable auth. Ask the user to install the GitHub App on the relevant repos.",
            })
        return json.dumps({"repos": repos})
    except Exception as e:
        logger.exception(f"Error fetching connected repos: {e}")
        return json.dumps({"error": f"Failed to fetch connected repos: {e}"})
