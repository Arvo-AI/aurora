"""
Shared utilities for VCS (GitHub/GitLab) RCA tools.

Extracts common logic used by both github_rca_tool and gitlab_tool
to avoid duplication: repository resolution, time window calculation,
and correlation hint generation.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, Tuple, List

from utils.db.connection_pool import db_pool
from utils.auth.stateless_auth import set_rls_context

logger = logging.getLogger(__name__)


def resolve_repository(
    user_id: str,
    provider: str,
    explicit_repo: Optional[str] = None,
) -> Tuple[Optional[str], str]:
    """
    Resolve repository/project path from DB for a given provider.

    Args:
        user_id: The user whose connected repos to search
        provider: 'github' or 'gitlab'
        explicit_repo: If provided, returned directly without DB lookup

    Returns:
        (repo_path_or_None, source_description)
        For GitHub: repo_path is 'owner/repo'
        For GitLab: repo_path is 'namespace/project'
    """
    if explicit_repo:
        if provider == "github":
            parts = explicit_repo.split('/')
            if len(parts) == 2:
                return explicit_repo, "explicit parameter"
            logger.warning("Invalid GitHub repo format (expected owner/repo): %s", explicit_repo)
            return None, f"invalid repo format: expected 'owner/repo', got '{explicit_repo}'"
        else:
            # GitLab paths can be multi-segment (group/subgroup/project)
            if not explicit_repo.strip() or explicit_repo.startswith("/") or explicit_repo.endswith("/"):
                logger.warning("Suspicious %s repo path: %s", provider, explicit_repo)
            return explicit_repo, "explicit parameter"

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix=f"[{provider.title()}RCA:resolve]")
                cur.execute(
                    "SELECT repo_full_name FROM connected_repos WHERE user_id = %s AND provider = %s",
                    (user_id, provider),
                )
                rows = cur.fetchall()

        if not rows:
            return None, "no repository found"

        if len(rows) == 1:
            return rows[0][0], "connected repository"

        repo_list = ", ".join(r[0] for r in rows)
        tool_name = "get_connected_repos" if provider == "github" else "gitlab"
        action_hint = " and pass repo= explicitly" if provider == "github" else " with action='list_projects' and pass repo= explicitly"
        return None, f"multiple repos connected ({repo_list}). Call {tool_name}{action_hint}"

    except Exception as e:
        logger.warning(f"Error resolving {provider} repository: {e}")
        return None, f"database error: {e}"


def calculate_time_windows(
    incident_time: Optional[str],
    time_window_hours: int = 24,
) -> Tuple[datetime, datetime]:
    """
    Calculate investigation time windows based on incident time.

    Returns: (start_time, end_time)
    """
    if not isinstance(time_window_hours, int) or time_window_hours <= 0:
        logger.warning(f"Invalid time_window_hours={time_window_hours}, using default of 24")
        time_window_hours = 24

    if incident_time:
        try:
            incident_time_clean = incident_time.replace('Z', '+00:00')
            end_time = datetime.fromisoformat(incident_time_clean)
            if end_time.tzinfo is None:
                end_time = end_time.replace(tzinfo=timezone.utc)
        except ValueError as e:
            logger.warning(f"Could not parse incident_time '{incident_time}': {e}, using current time")
            end_time = datetime.now(timezone.utc)
    else:
        end_time = datetime.now(timezone.utc)

    start_time = end_time - timedelta(hours=time_window_hours)
    return start_time, end_time


def generate_correlation_hints(action: str, results: Dict[str, Any]) -> List[str]:
    """Generate hints to help correlate findings with incident."""
    hints = []

    if action in ("deployment_check",):
        failed_key = "failed_runs" if "failed_runs" in results else "failed_pipelines"
        suspicious_key = "suspicious_runs" if "suspicious_runs" in results else "suspicious_pipelines"
        if results.get(failed_key):
            hints.append(f"Found {len(results[failed_key])} FAILED runs/pipelines in time window - investigate these first")
        if results.get(suspicious_key):
            hints.append(f"Found {len(results[suspicious_key])} runs/pipelines completed within 2 hours of incident")

    elif action == "commits":
        if results.get("suspicious_commits"):
            hints.append(f"Found {len(results['suspicious_commits'])} commits within 2 hours of incident - high priority for review")
        total = results.get("summary", {}).get("total_commits", 0)
        if total > 10:
            hints.append(f"High commit activity ({total} commits) - consider narrowing time window")

    elif action == "diff":
        summary = results.get("summary", {})
        if summary.get("total_changes", 0) > 100:
            hints.append(f"Large change ({summary.get('total_changes')} lines) - review carefully")
        files = results.get("files_changed", [])
        config_files = [f for f in files if any(ext in f.get("filename", "").lower()
                       for ext in ['.yaml', '.yml', '.json', '.env', 'config', 'k8s/', 'deploy/', 'terraform/'])]
        if config_files:
            hints.append(f"Found {len(config_files)} config/infra files changed - likely candidates for root cause")

    elif action in ("pull_requests", "merge_requests"):
        if results.get("recently_merged"):
            hints.append(f"Found {len(results['recently_merged'])} PRs/MRs merged within 2 hours of incident")

    return hints


def get_connected_repos_for_provider(user_id: str, provider: str) -> str:
    """
    Fetch connected repositories/projects for a provider.

    Returns JSON string with list of repos or error message.
    Works for both 'github' and 'gitlab' (or any future VCS provider).
    """
    if not user_id:
        return json.dumps({"error": "No user context available"})

    provider_label = "GitHub repos" if provider == "github" else "GitLab projects"

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix=f"[{provider.title()}Repos:list]")
                cur.execute(
                    """SELECT repo_full_name, default_branch, is_private, metadata_summary, metadata_status
                       FROM connected_repos
                       WHERE user_id = %s AND provider = %s
                       ORDER BY repo_full_name""",
                    (user_id, provider),
                )
                rows = cur.fetchall()

        if not rows:
            connector_name = "GitHub" if provider == "github" else "GitLab"
            return json.dumps({
                "repos": [],
                "message": f"No {provider_label} connected. Ask the user to connect repos in Settings > Connectors > {connector_name}.",
            })

        key_name = "repo" if provider == "github" else "project"
        repos = [
            {
                key_name: r[0],
                "branch": r[1] or "main",
                "private": r[2],
                "description": r[3] or (
                    "(description generating...)" if r[4] != 'ready' else "(no description)"
                ),
            }
            for r in rows
        ]
        return json.dumps({"repos": repos})
    except Exception as e:
        logger.error(f"Error fetching connected {provider_label}: {e}", exc_info=True)
        return json.dumps({"error": f"Failed to fetch connected {provider_label}: {e}"})
