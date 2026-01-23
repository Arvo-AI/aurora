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
        from ..cloud_tools import get_websocket_context, send_websocket_message

        websocket_sender, event_loop = get_websocket_context()
        if websocket_sender and event_loop:
            message_data = {
                "type": "toast_notification",
                "data": {
                    "title": "Connect GitHub for CI/CD",
                    "description": "Connect your GitHub account to Aurora to automatically push IaC changes to your repository for CI/CD workflows.",
                    "variant": "default",
                    "duration": 12000,
                    "action": {
                        "label": "Connect GitHub",
                        "onClick": "open_connectors",
                    },
                    "timestamp": str(time.time()),
                },
            }
            send_websocket_message(message_data, "toast_notification")
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
        from ..mcp_tools import get_user_cloud_credentials

        repo = "user/repository"
        branch = "main"

        try:
            credentials = get_user_cloud_credentials(user_id)

            if credentials and "github_repo_selection" in credentials:
                repo_selection = credentials["github_repo_selection"]

                if repo_selection.get("repository") and repo_selection.get("branch"):
                    repo_data = repo_selection["repository"]
                    branch_data = repo_selection["branch"]

                    repo = repo_data.get("full_name", repo)
                    branch = branch_data.get("name", branch)

                    logger.info(
                        f"Found stored GitHub selection for user {user_id}: {repo} / {branch}"
                    )
                else:
                    logger.info(
                        f"No GitHub repo selection found for user {user_id}, using defaults"
                    )
            else:
                logger.info(
                    f"No GitHub repo selection found for user {user_id}, using defaults"
                )
        except Exception as e:
            logger.warning(f"Error fetching GitHub repo selection: {e}")

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
