"""Shared utilities for Bitbucket agent tools."""

import json
import logging
from typing import Optional

from utils.db.connection_pool import db_pool
from utils.auth.stateless_auth import set_rls_context, get_credentials_from_db
from connectors.bitbucket_connector.api_client import BitbucketAPIClient
from connectors.bitbucket_connector.oauth_utils import refresh_token_if_needed
from utils.auth.token_management import store_tokens_in_db
from utils.secrets.secret_ref_utils import get_token_owner_id
from utils.auth.command_gate import gate_action

logger = logging.getLogger(__name__)

DIFF_TRUNCATE_LIMIT = 50_000


def get_bb_client_for_user(user_id: str):
    """Get a BitbucketAPIClient with auto-refreshed OAuth tokens.

    Returns:
        BitbucketAPIClient instance, or None if not connected.
    """
    try:
        bb_creds = get_credentials_from_db(user_id, "bitbucket")
        if not bb_creds:
            return None

        auth_type = bb_creds.get("auth_type", "oauth")
        access_token = bb_creds.get("access_token")
        if not access_token:
            return None

        # Refresh OAuth tokens if needed
        if auth_type == "oauth":
            old_access_token = access_token
            bb_creds = refresh_token_if_needed(bb_creds)
            access_token = bb_creds.get("access_token", access_token)

            # Persist refreshed token if changed
            if access_token != old_access_token:
                try:
                    owner_id = get_token_owner_id(user_id, "bitbucket")
                    store_tokens_in_db(owner_id, bb_creds, "bitbucket")
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
        creds = get_credentials_from_db(user_id, "bitbucket")
        return bool(creds and creds.get("access_token"))
    except Exception as e:
        logger.warning(f"Error checking Bitbucket connection: {e}")
        return False


def get_default_branch(user_id: str, workspace: str, repo_slug: str) -> Optional[str]:
    """Look up the default branch for a connected Bitbucket repo."""
    try:
        full_name = f"{workspace}/{repo_slug}"
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[BitbucketTools:branch]")
                cur.execute(
                    "SELECT default_branch FROM connected_repos WHERE provider = 'bitbucket' AND repo_full_name = %s LIMIT 1",
                    (full_name,),
                )
                row = cur.fetchone()
                if row and row[0]:
                    return row[0]
    except Exception as e:
        logger.warning(f"Failed to look up default branch for {workspace}/{repo_slug}: {e}")
    return None


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


def build_cancelled_response() -> str:
    """Build the standard cancellation response for a rejected confirmation."""
    return build_success_response(message="Operation cancelled by user", cancelled=True)


def confirm_or_cancel(user_id: str, message: str, tool_name: str) -> Optional[str]:
    """Request human approval for a destructive action.

    Returns ``None`` if approved, or a JSON cancellation response string
    if the user declines. Delegates to the unified command gate so
    Bitbucket confirmations share the same UI/WS/taint plumbing as the
    shell-command gate.
    """
    if gate_action(user_id=user_id, tool_name=tool_name, summary=message).allowed:
        return None
    return build_cancelled_response()
