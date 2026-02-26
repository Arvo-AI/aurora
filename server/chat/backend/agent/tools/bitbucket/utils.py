"""Shared utilities for Bitbucket agent tools."""

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

DIFF_TRUNCATE_LIMIT = 50_000


def _extract_field(value, field: str, default=None):
    """Extract a field from a value that may be a dict or a plain string.

    Handles the common Bitbucket pattern where stored selections can be
    either a dict (``{"slug": "foo", ...}``) or a plain string (``"foo"``).
    """
    if isinstance(value, dict):
        return value.get(field, default)
    return value if value is not None else default


def get_bb_client_for_user(user_id: str):
    """Get a BitbucketAPIClient with auto-refreshed OAuth tokens.

    Returns:
        BitbucketAPIClient instance, or None if not connected.
    """
    try:
        from utils.auth.stateless_auth import get_credentials_from_db
        from connectors.bitbucket_connector.api_client import BitbucketAPIClient

        bb_creds = get_credentials_from_db(user_id, "bitbucket")
        if not bb_creds:
            return None

        auth_type = bb_creds.get("auth_type", "oauth")
        access_token = bb_creds.get("access_token")
        if not access_token:
            return None

        # Refresh OAuth tokens if needed
        if auth_type == "oauth":
            from connectors.bitbucket_connector.oauth_utils import refresh_token_if_needed

            old_access_token = access_token
            bb_creds = refresh_token_if_needed(bb_creds)
            access_token = bb_creds.get("access_token", access_token)

            # Persist refreshed token if changed
            if access_token != old_access_token:
                try:
                    from utils.auth.token_management import store_tokens_in_db
                    store_tokens_in_db(user_id, bb_creds, "bitbucket")
                    logger.info("Persisted refreshed Bitbucket token")
                except Exception as e:
                    logger.warning(f"Failed to persist refreshed Bitbucket token: {e}")

        email = bb_creds.get("email")
        return BitbucketAPIClient(access_token, auth_type=auth_type, email=email)

    except Exception as e:
        logger.error(f"Failed to get Bitbucket client: {e}", exc_info=True)
        return None


def is_bitbucket_connected(user_id: str) -> bool:
    """Check if Bitbucket credentials exist for a user."""
    try:
        from utils.auth.stateless_auth import get_credentials_from_db
        creds = get_credentials_from_db(user_id, "bitbucket")
        return bool(creds and creds.get("access_token"))
    except Exception as e:
        logger.warning(f"Error checking Bitbucket connection: {e}")
        return False


def resolve_workspace_repo(
    user_id: str,
    workspace: Optional[str] = None,
    repo_slug: Optional[str] = None,
) -> tuple[Optional[str], Optional[str], Optional[str], str]:
    """Resolve workspace, repo, and branch from explicit params or stored selection.

    Priority: 1. Explicit params  2. Saved selection from DB

    Returns:
        (workspace, repo_slug, branch, source_description)
    """
    source = "explicit"
    branch = None

    if not workspace or not repo_slug:
        try:
            from utils.auth.stateless_auth import get_credentials_from_db
            selection = get_credentials_from_db(user_id, "bitbucket_workspace_selection") or {}

            if not workspace:
                workspace = _extract_field(selection.get("workspace"), "slug")

            if not repo_slug:
                repo_slug = _extract_field(selection.get("repository"), "slug")

            branch = _extract_field(selection.get("branch"), "name")
            source = "saved selection"
        except Exception as e:
            logger.warning(f"Failed to load Bitbucket workspace selection: {e}")

    return workspace, repo_slug, branch, source


def require_repo(ws: Optional[str], repo: Optional[str]) -> Optional[str]:
    """Return an error message if workspace or repo is missing, else None."""
    if not ws or not repo:
        return "workspace and repo_slug are required"
    return None


def forward_if_error(result) -> Optional[str]:
    """Return a JSON string if the result is an API error dict, else None."""
    if isinstance(result, dict) and result.get("error") is True:
        return json.dumps(result, default=str)
    return None


def truncate_text(text: str, limit: int, label: str = "output") -> str:
    """Truncate text to a maximum length with an informative suffix."""
    if len(text) <= limit:
        return text
    size_kb = limit // 1000
    return text[:limit] + f"\n... [{label} truncated at {size_kb}KB]"


def build_error_response(message: str, **kwargs) -> str:
    """Build a JSON error response string."""
    result = {"error": True, "message": message}
    result.update(kwargs)
    return json.dumps(result)


def build_success_response(**kwargs) -> str:
    """Build a JSON success response string."""
    result = {"success": True}
    result.update(kwargs)
    return json.dumps(result, default=str)


def get_session_id():
    """Get session_id from the current agent state context."""
    from chat.backend.agent.tools.cloud_tools import get_state_context

    return getattr(get_state_context(), "session_id", None)


def build_cancelled_response() -> str:
    """Build the standard cancellation response for a rejected confirmation."""
    return build_success_response(message="Operation cancelled by user", cancelled=True)


def confirm_or_cancel(user_id: str, message: str, tool_name: str) -> Optional[str]:
    """Request human approval for a destructive action.

    Retrieves the session ID automatically and prompts the user for
    confirmation. Returns ``None`` if approved, or a JSON cancellation
    response string if the user declines.
    """
    from utils.cloud.infrastructure_confirmation import wait_for_user_confirmation

    session_id = get_session_id()
    approved = wait_for_user_confirmation(
        user_id=user_id,
        message=message,
        tool_name=tool_name,
        session_id=session_id,
    )
    if not approved:
        return build_cancelled_response()
    return None
