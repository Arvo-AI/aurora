"""
User interaction flows for Infrastructure as Code operations.
Handles confirmations, GitHub integration, and notifications.
Shared by both Terraform and future Pulumi support.
"""

import logging
import time
from typing import Any, Dict

logger = logging.getLogger(__name__)


def check_github_connection(user_id: str) -> bool:
    """
    Check if the user has connected their GitHub account.
    Shared utility for both Terraform and Pulumi workflows.
    """
    try:
        from ..mcp_tools import get_user_cloud_credentials

        credentials = get_user_cloud_credentials(user_id)
        if credentials and credentials.get("github", {}).get("access_token"):
            logger.info(f"GitHub is connected for user {user_id}")
            return True
        logger.info(f"GitHub is not connected for user {user_id}")
        return False
    except Exception as e:
        logger.warning(f"Error checking GitHub connection: {e}")
        return False


def send_github_connection_toast(user_id: str) -> None:
    """
    Send a toast notification prompting the user to connect GitHub.
    Used after successful IaC deployments to encourage CI/CD setup.
    """
    try:
        from ..cloud_tools import (
            _emit_event,
            get_websocket_context,
            send_websocket_message,
        )

        toast_payload = {
            "title": "Connect GitHub for CI/CD",
            "description": "Connect your GitHub account to Aurora to automatically push IaC changes to your repository for CI/CD workflows.",
            "variant": "default",
            "duration": 12000,
            "action": {
                "label": "Connect GitHub",
                "onClick": "open_connectors",
            },
            "timestamp": str(time.time()),
        }

        websocket_sender, event_loop = get_websocket_context()
        if websocket_sender and event_loop:
            send_websocket_message(
                {"type": "toast_notification", "data": toast_payload},
                "toast_notification",
            )

        _emit_event("toast_notification", toast_payload)
        logger.info(f"Sent GitHub connection toast notification to user {user_id}")
    except Exception as e:
        logger.warning(f"Failed to send GitHub connection toast notification: {e}")


def prepare_github_commit_suggestion(
    user_id: str, session_id: str, terraform_dir: str
) -> Dict[str, Any]:
    """
    Prepare suggestion for GitHub commit after successful IaC apply.
    Works for both Terraform and will work for Pulumi in the future.
    
    Args:
        user_id: User identifier
        session_id: Session identifier
        terraform_dir: Directory containing IaC files
        
    Returns:
        Dict with commit suggestion details or error status
    """
    try:
        repo = "user/repository"
        branch = "main"

        try:
            from utils.db.connection_pool import db_pool
            from utils.auth.stateless_auth import set_rls_context
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cur:
                    set_rls_context(cur, conn, user_id, log_prefix="[IaC:repos]")
                    cur.execute(
                        """SELECT repo_full_name, default_branch
                           FROM github_connected_repos
                           WHERE user_id = %s ORDER BY created_at DESC LIMIT 1""",
                        (user_id,),
                    )
                    row = cur.fetchone()
            if row:
                repo = row[0]
                branch = row[1] or "main"
                logger.info(f"Found GitHub repo for user {user_id}: {repo} / {branch}")
            else:
                logger.info(f"No GitHub repos connected for user {user_id}, using defaults")
        except Exception as e:
            logger.warning(f"Error fetching GitHub repo: {e}")

        commit_message = f"Apply Terraform changes from Aurora session {session_id[:8]}"

        return {
            "status": "ready_for_commit",
            "repo": repo,
            "branch": branch,
            "suggested_commit_message": commit_message,
            "terraform_directory": str(terraform_dir),
        }

    except Exception as e:
        logger.error(f"Error preparing GitHub commit suggestion: {e}")
        return {"status": "error", "error": str(e)}


__all__ = [
    "check_github_connection",
    "send_github_connection_toast",
    "prepare_github_commit_suggestion",
]
