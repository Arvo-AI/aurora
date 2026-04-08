"""
Google Chat OAuth routes for Aurora integration.
Handles OAuth flow, connection status, and disconnection.
"""

import logging
import os
import time
from urllib.parse import quote
from flask import Blueprint, request, jsonify, redirect
from connectors.google_chat_connector.oauth import (
    get_auth_url,
    exchange_code_for_token,
    get_user_info,
)
from connectors.google_chat_connector.client import (
    create_incidents_space,
    get_google_chat_client_for_user,
)
from utils.auth.stateless_auth import get_credentials_from_db
from utils.secrets.secret_ref_utils import delete_user_secret
from utils.auth.token_management import store_tokens_in_db
from utils.auth.rbac_decorators import require_permission

google_chat_bp = Blueprint("google_chat", __name__)

FRONTEND_URL = os.getenv("FRONTEND_URL")


@google_chat_bp.route("/env/check", methods=["GET"])
@require_permission("connectors", "read")
def google_chat_env_check(_user_id):
    """GET /google-chat/env/check - Check if Google Chat env vars are configured."""
    client_id = os.getenv("GOOGLE_CHAT_CLIENT_ID", "")
    client_secret = os.getenv("GOOGLE_CHAT_CLIENT_SECRET", "")
    project_number = os.getenv("GOOGLE_CHAT_PROJECT_NUMBER", "")
    verification_token = os.getenv("GOOGLE_CHAT_VERIFICATION_TOKEN", "")

    ngrok_url = os.getenv("NGROK_URL", "").rstrip("/")
    backend_url = os.getenv("NEXT_PUBLIC_BACKEND_URL", "").rstrip("/")
    base_url = ngrok_url if ngrok_url and backend_url.startswith("http://localhost") else backend_url

    return jsonify({
        "configured": bool(client_id and client_secret and project_number and verification_token),
        "hasClientId": bool(client_id),
        "hasClientSecret": bool(client_secret),
        "hasProjectNumber": bool(project_number),
        "hasVerificationToken": bool(verification_token),
        "baseUrl": base_url,
    })


@google_chat_bp.route("/", methods=["GET"], strict_slashes=False)
@require_permission("connectors", "read")
def google_chat_status(user_id):
    """GET /google-chat - Get connection status."""
    try:
        creds = get_credentials_from_db(user_id, "google_chat")
        if not creds or not creds.get("access_token"):
            return jsonify({"connected": False})

        client = get_google_chat_client_for_user(user_id)
        if not client:
            return jsonify({"connected": False, "error": "Invalid or expired credentials"})

        return jsonify({
            "connected": True,
            "connected_by": creds.get("user_email"),
            "user_name": creds.get("user_name"),
            "domain": creds.get("domain"),
            "connected_at": creds.get("connected_at"),
            "incidents_space_display_name": creds.get("incidents_space_display_name"),
        })

    except Exception as e:
        logging.error(f"Error checking Google Chat status: {e}", exc_info=True)
        return jsonify({"connected": False, "error": "Failed to check status"}), 500


@google_chat_bp.route("/", methods=["POST"], strict_slashes=False)
@require_permission("connectors", "write")
def google_chat_connect(user_id):
    """POST /google-chat - Initiate OAuth connection (returns oauth_url)."""
    try:
        oauth_url = get_auth_url(state=user_id)
        return jsonify({
            "oauth_url": oauth_url,
            "message": "Redirect to Google for authentication",
        })
    except ValueError as e:
        error_msg = str(e)
        if "not configured" in error_msg.lower():
            return jsonify({
                "error": "Google Chat OAuth is not configured. Set GOOGLE_CHAT_CLIENT_ID and GOOGLE_CHAT_CLIENT_SECRET in your .env file, then restart Aurora.",
                "error_code": "GOOGLE_CHAT_NOT_CONFIGURED",
            }), 400
        logging.error(f"Error initiating Google Chat OAuth: {e}", exc_info=True)
        return jsonify({"error": "Failed to initiate Google Chat OAuth"}), 400
    except Exception as e:
        logging.error(f"Error initiating Google Chat OAuth: {e}", exc_info=True)
        return jsonify({"error": "Failed to initiate Google Chat OAuth"}), 500


@google_chat_bp.route("/", methods=["DELETE"], strict_slashes=False)
@require_permission("connectors", "write")
def google_chat_disconnect(user_id):
    """DELETE /google-chat - Disconnect Google Chat."""
    try:
        delete_success = delete_user_secret(user_id, "google_chat")
        if delete_success:
            logging.info(f"Disconnected Google Chat for user {user_id}")
            return jsonify({"success": True, "message": "Google Chat disconnected"})
        else:
            return jsonify({"error": "Failed to disconnect Google Chat"}), 500
    except Exception as e:
        logging.error(f"Error disconnecting Google Chat: {e}", exc_info=True)
        return jsonify({"error": "Failed to disconnect Google Chat"}), 500


@google_chat_bp.route("/callback", methods=["GET", "POST"])
def google_chat_callback():
    """Handle the OAuth callback from Google."""
    try:
        code = request.args.get("code")
        state = request.args.get("state")

        if not code or not state:
            logging.error("No code or state provided in Google Chat callback")
            return redirect(f"{FRONTEND_URL}?google_chat_auth=failed&error=no_code_or_state")

        user_id = state

        try:
            token_data = exchange_code_for_token(code)
        except Exception as e:
            logging.error(f"Token exchange failed: {e}", exc_info=True)
            return redirect(f"{FRONTEND_URL}?google_chat_auth=failed&error=token_exchange_failed")

        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")

        if not access_token:
            logging.error(f"No access token in Google response: {token_data}")
            return redirect(f"{FRONTEND_URL}?google_chat_auth=failed&error=no_token")

        # Get user info from Google
        try:
            user_info = get_user_info(access_token)
        except Exception as e:
            logging.error(f"Failed to get user info: {e}", exc_info=True)
            user_info = {}

        user_email = user_info.get("email", "")
        user_name = user_info.get("name", "")
        domain = user_email.split("@")[1] if "@" in user_email else ""

        # Create incidents space
        space_result = create_incidents_space(
            access_token, domain or "your organization", user_email,
        )
        if not space_result.get("ok"):
            error_msg = space_result.get("error", "Unknown error")
            logging.error(f"Failed to create incidents space: {error_msg}")
            return redirect(
                f"{FRONTEND_URL}?google_chat_auth=failed&error=space_creation_failed"
            )

        # Store credentials
        try:
            google_chat_token_data = {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "user_email": user_email,
                "user_name": user_name,
                "domain": domain,
                "connected_at": int(time.time()),
                "incidents_space_name": space_result.get("space_name"),
                "incidents_space_display_name": space_result.get("space_display_name"),
                "expires_in": token_data.get("expires_in"),
                "token_type": token_data.get("token_type"),
            }

            store_tokens_in_db(user_id, google_chat_token_data, "google_chat")
            logging.info(
                f"Google Chat connected for {user_email}, "
                f"space: {space_result.get('space_display_name')}"
            )

        except Exception as e:
            logging.error(f"Failed to store Google Chat credentials: {e}", exc_info=True)
            return redirect(f"{FRONTEND_URL}?google_chat_auth=failed&error=storage_failed")

        return redirect(
            f"{FRONTEND_URL}?google_chat_auth=success&domain={quote(domain, safe='')}"
        )

    except Exception as e:
        logging.error(f"Error during Google Chat callback: {e}", exc_info=True)
        return redirect(f"{FRONTEND_URL}?google_chat_auth=failed&error=unexpected_error")
