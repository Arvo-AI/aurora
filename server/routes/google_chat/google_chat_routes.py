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
        logger.error("Error checking Google Chat status", exc_info=True)
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
        has_service_account = bool(os.getenv("GOOGLE_CHAT_SERVICE_ACCOUNT_KEY"))
        if not has_client_id or not has_client_secret or not has_service_account:
            return jsonify({
                "error": "Google Chat is not fully configured. "
                         "Set GOOGLE_CHAT_CLIENT_ID, GOOGLE_CHAT_CLIENT_SECRET, "
                         "and GOOGLE_CHAT_SERVICE_ACCOUNT_KEY in your .env file, "
                         "then restart Aurora.",
                "error_code": "GOOGLE_CHAT_NOT_CONFIGURED",
            }), 400

        state = secrets.token_urlsafe(32)
        store_oauth2_state(state, user_id, "google_chat")
        auth_url = get_auth_url(state)

        logger.info("Generated Google Chat OAuth URL for user %s", user_id)
        return jsonify({"oauth_url": auth_url})

    except Exception as e:
        logger.error("Error starting Google Chat OAuth", exc_info=True)
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

    if not FRONTEND_URL:
        logger.error("FRONTEND_URL is not set — cannot complete OAuth callback")
        return jsonify({"error": "Server misconfigured"}), 500

    setup_page = f"{FRONTEND_URL.rstrip('/')}/google-chat/setup"

    ERROR_MAP = {
        "access_denied": "access_denied",
        "invalid_scope": "invalid_scope",
        "server_error": "server_error",
        "temporarily_unavailable": "temporarily_unavailable",
        "invalid_request": "invalid_request",
    }

    if error:
        safe_error = ERROR_MAP.get(error, "oauth_error")
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
            SETUP_ERROR_MAP = {
                "space_creation_failed": "space_creation_failed",
                "space_not_resolved": "space_not_resolved",
                "insufficient_permissions": "insufficient_permissions",
                "setup_failed": "setup_failed",
                "app_install_failed": "app_install_failed",
            }
            safe_code = SETUP_ERROR_MAP.get(error_code, "setup_failed")
            logger.error("Failed to create incidents space: %s", safe_code)
            return redirect(f"{setup_page}?error={quote(safe_code)}")

        google_chat_config = {
            "connected_by": user_id,
            "connected_at": int(time.time()),
            "incidents_space_name": space_result.get("space_name"),
            "incidents_space_display_name": space_result.get("space_display_name"),
        }
        store_tokens_in_db(user_id, google_chat_config, "google_chat")

        logger.info("Google Chat connected for user %s", user_id)

        return redirect(f"{setup_page}?success=true")

    except Exception as e:
        logger.error("Google Chat callback error", exc_info=True)
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
        logger.error("Error disconnecting Google Chat", exc_info=True)
        return jsonify({"error": "Failed to disconnect Google Chat"}), 500


def _get_org_space_config(user_id: str):
    """Retrieve the stored Google Chat space config for the user's org."""
    try:
        from utils.auth.stateless_auth import get_credentials_from_db
        return get_credentials_from_db(user_id, "google_chat")
    except Exception as e:
        logger.debug("Failed to retrieve Google Chat space config for user %s: %s", user_id, e)
        return None


def _get_user_org_id(user_id: str) -> str | None:
    """Return the org_id for a user, or None."""
    try:
        from utils.db.connection_pool import db_pool
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT org_id FROM users WHERE id = %s", (user_id,))
                row = cur.fetchone()
                return row[0] if row else None
    except Exception as e:
        logger.warning("Error fetching org_id for user %s: %s", user_id, e)
        return None


# ── Team ↔ Space mappings ──────────────────────────────────────────────


@google_chat_bp.route("/spaces/bot", methods=["GET"])
@require_permission("connectors", "read")
def list_bot_spaces(user_id):
    """GET /google-chat/spaces/bot — spaces the Aurora Chat app is a member of."""
    client = get_chat_app_client()
    if not client:
        return jsonify({"error": "Service account not configured"}), 400
    try:
        spaces = client.list_bot_spaces_summary()
        return jsonify({"spaces": spaces})
    except Exception as e:
        logger.error("Error listing bot spaces", exc_info=True)
        return jsonify({"error": "Failed to list spaces"}), 500


@google_chat_bp.route("/team-mappings", methods=["GET"])
@require_permission("connectors", "read")
def get_team_mappings(user_id):
    """GET /google-chat/team-mappings — all team→space mappings for the org."""
    from utils.db.connection_pool import db_pool

    org_id = _get_user_org_id(user_id)
    if not org_id:
        return jsonify({"error": "No org found for user"}), 400

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, team_name, space_name, space_display_name,
                              description, created_by, created_at
                       FROM gchat_team_space_mappings
                       WHERE org_id = %s ORDER BY team_name""",
                    (org_id,),
                )
                rows = cur.fetchall()

        mappings = [
            {
                "id": r[0],
                "team_name": r[1],
                "space_name": r[2],
                "space_display_name": r[3],
                "description": r[4],
                "created_by": r[5],
                "created_at": r[6].isoformat() if r[6] else None,
            }
            for r in rows
        ]
        return jsonify({"mappings": mappings})
    except Exception as e:
        logger.error("Error getting team mappings", exc_info=True)
        return jsonify({"error": "Failed to get team mappings"}), 500


@google_chat_bp.route("/team-mappings", methods=["POST"])
@require_permission("connectors", "write")
def upsert_team_mapping(user_id):
    """POST /google-chat/team-mappings — create or update a team→space mapping."""
    from utils.db.connection_pool import db_pool

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    team_name = (data.get("team_name") or "").strip()
    space_name = (data.get("space_name") or "").strip()
    space_display_name = (data.get("space_display_name") or "").strip() or None
    description = (data.get("description") or "").strip() or None

    if not team_name or not space_name:
        return jsonify({"error": "team_name and space_name are required"}), 400

    org_id = _get_user_org_id(user_id)
    if not org_id:
        return jsonify({"error": "No org found for user"}), 400

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO gchat_team_space_mappings
                           (org_id, team_name, space_name, space_display_name,
                            description, created_by)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       ON CONFLICT (org_id, team_name) DO UPDATE SET
                           space_name = EXCLUDED.space_name,
                           space_display_name = EXCLUDED.space_display_name,
                           description = EXCLUDED.description,
                           updated_at = NOW()
                       RETURNING id""",
                    (org_id, team_name, space_name, space_display_name,
                     description, user_id),
                )
                row = cur.fetchone()
                conn.commit()

        return jsonify({"id": row[0], "message": f"Mapped '{team_name}' → {space_name}"})
    except Exception as e:
        logger.error("Error upserting team mapping", exc_info=True)
        return jsonify({"error": "Failed to save team mapping"}), 500


@google_chat_bp.route("/team-mappings/<int:mapping_id>", methods=["DELETE"])
@require_permission("connectors", "write")
def delete_team_mapping(user_id, mapping_id):
    """DELETE /google-chat/team-mappings/<id> — remove a mapping."""
    from utils.db.connection_pool import db_pool

    org_id = _get_user_org_id(user_id)
    if not org_id:
        return jsonify({"error": "No org found for user"}), 400

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM gchat_team_space_mappings WHERE id = %s AND org_id = %s",
                    (mapping_id, org_id),
                )
                if cur.rowcount == 0:
                    return jsonify({"error": "Mapping not found"}), 404
                conn.commit()
        return jsonify({"message": "Mapping deleted"})
    except Exception as e:
        logger.error("Error deleting team mapping", exc_info=True)
        return jsonify({"error": "Failed to delete mapping"}), 500


# ── Routing instructions ───────────────────────────────────────────────


@google_chat_bp.route("/routing-instructions", methods=["GET"])
@require_permission("connectors", "read")
def get_routing_instructions(user_id):
    """GET /google-chat/routing-instructions — org-level routing instructions."""
    from utils.db.connection_pool import db_pool

    org_id = _get_user_org_id(user_id)
    if not org_id:
        return jsonify({"error": "No org found for user"}), 400

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT routing_instructions FROM gchat_routing_config WHERE org_id = %s",
                    (org_id,),
                )
                row = cur.fetchone()
        return jsonify({"routing_instructions": row[0] if row else ""})
    except Exception as e:
        logger.error("Error getting routing instructions", exc_info=True)
        return jsonify({"error": "Failed to get routing instructions"}), 500


@google_chat_bp.route("/routing-instructions", methods=["PUT"])
@require_permission("connectors", "write")
def update_routing_instructions(user_id):
    """PUT /google-chat/routing-instructions — save org-level routing instructions."""
    from utils.db.connection_pool import db_pool

    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"error": "JSON body required"}), 400

    instructions = (data.get("routing_instructions") or "").strip()

    org_id = _get_user_org_id(user_id)
    if not org_id:
        return jsonify({"error": "No org found for user"}), 400

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO gchat_routing_config (org_id, routing_instructions, updated_by)
                       VALUES (%s, %s, %s)
                       ON CONFLICT (org_id) DO UPDATE SET
                           routing_instructions = EXCLUDED.routing_instructions,
                           updated_by = EXCLUDED.updated_by,
                           updated_at = NOW()""",
                    (org_id, instructions, user_id),
                )
                conn.commit()
        return jsonify({"message": "Routing instructions saved"})
    except Exception as e:
        logger.error("Error saving routing instructions", exc_info=True)
        return jsonify({"error": "Failed to save routing instructions"}), 500
