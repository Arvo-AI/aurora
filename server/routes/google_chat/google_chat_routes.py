"""
Google Chat routes for Aurora integration.

Hybrid auth model:
  - User OAuth is used for *setup only*: creating/finding the incidents space
    inside the customer's Google Workspace. Tokens are used once and discarded.
  - A GCP service account is used for all ongoing messaging so messages
    appear as "Aurora".
"""

import logging
import os
import secrets
import time
from urllib.parse import quote
from flask import Blueprint, request, jsonify, redirect
from connectors.google_chat_connector.client import (
    create_incidents_space,
    get_chat_app_client,
)
from connectors.google_chat_connector.oauth import (
    get_auth_url,
    exchange_code_for_token,
)
from utils.auth.token_management import store_tokens_in_db
from utils.auth.rbac_decorators import require_permission
from utils.auth.oauth2_state_cache import store_oauth2_state, retrieve_oauth2_state
from utils.secrets.secret_ref_utils import delete_user_secret

google_chat_bp = Blueprint("google_chat", __name__)

logger = logging.getLogger(__name__)

FRONTEND_URL = os.getenv("FRONTEND_URL")


@google_chat_bp.route("/env/check", methods=["GET"])
@require_permission("connectors", "read")
def google_chat_env_check(_user_id):
    """GET /google-chat/env/check - Check if Google Chat env vars are configured."""
    has_client_id = bool(os.getenv("GOOGLE_CHAT_CLIENT_ID"))
    has_client_secret = bool(os.getenv("GOOGLE_CHAT_CLIENT_SECRET"))
    has_service_account = bool(os.getenv("GOOGLE_CHAT_SERVICE_ACCOUNT_KEY"))

    ngrok_url = os.getenv("NGROK_URL", "").rstrip("/")
    backend_url = os.getenv("NEXT_PUBLIC_BACKEND_URL", "").rstrip("/")
    base_url = ngrok_url if ngrok_url and backend_url.startswith("http://localhost") else backend_url

    return jsonify({
        "configured": bool(has_client_id and has_client_secret and has_service_account),
        "hasClientId": has_client_id,
        "hasClientSecret": has_client_secret,
        "hasServiceAccount": has_service_account,
        "baseUrl": base_url,
    })


@google_chat_bp.route("/", methods=["GET"], strict_slashes=False)
@require_permission("connectors", "read")
def google_chat_status(user_id):
    """GET /google-chat - Get connection status."""
    try:
        space_config = _get_org_space_config(user_id)
        if not space_config or not space_config.get("incidents_space_name"):
            return jsonify({"connected": False})

        has_sa = get_chat_app_client() is not None
        return jsonify({
            "connected": has_sa,
            "has_service_account": has_sa,
            "connected_by": space_config.get("connected_by"),
            "connected_at": space_config.get("connected_at"),
            "incidents_space_display_name": space_config.get("incidents_space_display_name"),
        })

    except Exception as e:
        logger.error(f"Error checking Google Chat status: {e}", exc_info=True)
        return jsonify({"connected": False, "error": "Failed to check status"}), 500


@google_chat_bp.route("/", methods=["POST"], strict_slashes=False)
@require_permission("connectors", "write")
def google_chat_connect(user_id):
    """POST /google-chat - Start the OAuth flow for Google Chat setup.

    Returns an ``oauth_url`` that the frontend should redirect to.
    """
    try:
        has_client_id = bool(os.getenv("GOOGLE_CHAT_CLIENT_ID"))
        has_client_secret = bool(os.getenv("GOOGLE_CHAT_CLIENT_SECRET"))
        if not has_client_id or not has_client_secret:
            return jsonify({
                "error": "Google Chat OAuth credentials not configured. "
                         "Set GOOGLE_CHAT_CLIENT_ID and GOOGLE_CHAT_CLIENT_SECRET "
                         "in your .env file, then restart Aurora.",
                "error_code": "GOOGLE_CHAT_NOT_CONFIGURED",
            }), 400

        state = secrets.token_urlsafe(32)
        store_oauth2_state(state, user_id, "google_chat")
        auth_url = get_auth_url(state)

        logger.info(f"Generated Google Chat OAuth URL for user {user_id}")
        return jsonify({"oauth_url": auth_url})

    except Exception as e:
        logger.error(f"Error starting Google Chat OAuth: {e}", exc_info=True)
        return jsonify({"error": "Failed to start Google Chat setup"}), 500


@google_chat_bp.route("/callback", methods=["GET"])
def google_chat_callback():
    """GET /google-chat/callback - OAuth callback.

    1. Exchange code for tokens (user context).
    2. Create/find the incidents space in the customer's workspace.
    3. Store space config only (no tokens -- the service account handles messaging).
    4. Redirect to the frontend setup page.
    """
    code = request.args.get("code")
    state = request.args.get("state")
    error = request.args.get("error")

    setup_page = f"{FRONTEND_URL.rstrip('/')}/google-chat/setup"

    ALLOWED_ERRORS = {
        "access_denied", "invalid_scope", "server_error",
        "temporarily_unavailable", "invalid_request",
    }

    if error:
        safe_error = error if error in ALLOWED_ERRORS else "oauth_error"
        logger.warning("Google Chat OAuth error: %s", safe_error)
        return redirect(f"{setup_page}?error={quote(safe_error)}")

    if not code or not state:
        return redirect(f"{setup_page}?error=missing_params")

    state_data = retrieve_oauth2_state(state)
    if not state_data:
        return redirect(f"{setup_page}?error=invalid_state")

    user_id = state_data.get("user_id")
    if not user_id:
        return redirect(f"{setup_page}?error=invalid_state")

    try:
        token_data = exchange_code_for_token(code)
        access_token = token_data.get("access_token")

        if not access_token:
            return redirect(f"{setup_page}?error=no_access_token")

        space_result = create_incidents_space(access_token)
        if not space_result.get("ok"):
            error_code = space_result.get("error", "space_creation_failed")
            ALLOWED_SETUP_ERRORS = {
                "space_creation_failed", "space_not_resolved",
                "insufficient_permissions", "setup_failed", "app_install_failed",
            }
            safe_code = error_code if error_code in ALLOWED_SETUP_ERRORS else "setup_failed"
            logger.error(f"Failed to create incidents space: {error_code}")
            return redirect(f"{setup_page}?error={quote(safe_code)}")

        google_chat_config = {
            "connected_by": user_id,
            "connected_at": int(time.time()),
            "incidents_space_name": space_result.get("space_name"),
            "incidents_space_display_name": space_result.get("space_display_name"),
        }
        store_tokens_in_db(user_id, google_chat_config, "google_chat")

        logger.info("Google Chat connected for org (by user %s)", user_id)

        return redirect(f"{setup_page}?success=true")

    except Exception as e:
        logger.error(f"Google Chat callback error: {e}", exc_info=True)
        return redirect(f"{setup_page}?error=callback_failed")


@google_chat_bp.route("/", methods=["DELETE"], strict_slashes=False)
@require_permission("connectors", "write")
def google_chat_disconnect(user_id):
    """DELETE /google-chat - Disconnect Google Chat."""
    try:
        delete_success, deleted_rows = delete_user_secret(user_id, "google_chat")
        if delete_success:
            logger.info("Disconnected Google Chat for user %s (%s rows removed)", user_id, deleted_rows)
            return jsonify({"success": True, "message": "Google Chat disconnected"})
        else:
            return jsonify({"error": "Failed to disconnect Google Chat"}), 500
    except Exception as e:
        logger.error(f"Error disconnecting Google Chat: {e}", exc_info=True)
        return jsonify({"error": "Failed to disconnect Google Chat"}), 500


def _get_org_space_config(user_id: str):
    """Retrieve the stored Google Chat space config for the user's org."""
    try:
        from utils.auth.stateless_auth import get_credentials_from_db
        return get_credentials_from_db(user_id, "google_chat")
    except Exception as e:
        logger.debug(f"Failed to retrieve Google Chat space config for user {user_id}: {e}")
        return None
