"""Microsoft Teams OAuth routes."""

import logging
import os
import secrets
import time

import requests
from flask import Blueprint, jsonify, redirect, request

from connectors.teams_connector.client import get_teams_client_for_user
from connectors.teams_connector.oauth import exchange_code_for_token, get_auth_url
from routes.teams.teams_channels import purge_teams_connector_data
from utils.auth.oauth2_state_cache import retrieve_oauth2_state, store_oauth2_state
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_credentials_from_db
from utils.auth.token_management import store_tokens_in_db
from utils.secrets.secret_ref_utils import delete_user_secret

teams_bp = Blueprint("teams", __name__)
logger = logging.getLogger(__name__)
FRONTEND_URL = os.getenv("FRONTEND_URL")


@teams_bp.route("/", methods=["GET"], strict_slashes=False)
@require_permission("connectors", "read")
def teams_status(user_id):
    try:
        creds = get_credentials_from_db(user_id, "teams")
        if not creds or not creds.get("access_token"):
            return jsonify({"connected": False})
        client = get_teams_client_for_user(user_id)
        if not client:
            return jsonify({"connected": False, "error": "Invalid or expired token"})
        me = client.get_me()
        return jsonify({
            "connected": True,
            "tenant_id": creds.get("tenant_id"),
            "team_name": creds.get("team_name"),
            "user_name": me.get("displayName"),
            "connected_at": creds.get("connected_at"),
            "incidents_channel_name": creds.get("incidents_channel_name"),
        })
    except Exception:
        logger.exception("Error checking Teams status")
        return jsonify({"connected": False, "error": "Failed to check Teams status"}), 500


@teams_bp.route("/", methods=["POST"], strict_slashes=False)
@require_permission("connectors", "write")
def teams_connect(user_id):
    try:
        state = secrets.token_urlsafe(32)
        store_oauth2_state(state, user_id, "teams")
        return jsonify({"oauth_url": get_auth_url(state=state), "message": "Redirect to Microsoft for authentication"})
    except ValueError as exc:
        # Missing env vars or invalid connect params — safe to show the operator-facing text.
        logger.warning("Teams OAuth connect refused: %s", exc)
        return jsonify({"error": str(exc), "error_code": "TEAMS_OAUTH_NOT_CONFIGURED"}), 503
    except Exception:
        logger.exception("Error initiating Teams OAuth")
        return jsonify({"error": "Failed to initiate Teams OAuth"}), 500


@teams_bp.route("/", methods=["DELETE"], strict_slashes=False)
@require_permission("connectors", "write")
def teams_disconnect(user_id):
    try:
        if delete_user_secret(user_id, "teams"):
            try:
                purge_teams_connector_data(user_id)
            except Exception:
                logger.warning("Teams disconnect: failed to purge local channel data", exc_info=True)
            return jsonify({"success": True, "message": "Microsoft Teams disconnected"})
        return jsonify({"error": "Failed to disconnect Microsoft Teams"}), 500
    except Exception:
        logger.exception("Error disconnecting Teams")
        return jsonify({"error": "Failed to disconnect Microsoft Teams"}), 500


@teams_bp.route("/callback", methods=["GET"])
def teams_callback():
    try:
        code = request.args.get("code")
        state = request.args.get("state")
        if not code or not state:
            return redirect(f"{FRONTEND_URL}?teams_auth=failed&error=no_code_or_state")
        state_data = retrieve_oauth2_state(state)
        if not state_data:
            return redirect(f"{FRONTEND_URL}?teams_auth=failed&error=invalid_state")
        user_id = state_data.get("user_id")
        if not user_id:
            return redirect(f"{FRONTEND_URL}?teams_auth=failed&error=invalid_state")
        token_data = exchange_code_for_token(code)
        access_token = token_data.get("access_token")
        if not access_token:
            return redirect(f"{FRONTEND_URL}?teams_auth=failed&error=no_token")

        # Resolve tenant id from the token's id_token claims when present.
        tenant_id = token_data.get("tenant_id")
        if not tenant_id:
            tenant_id = _tenant_from_graph(access_token)

        teams_token_data = {
            "access_token": access_token,
            "refresh_token": token_data.get("refresh_token"),
            "expires_in": token_data.get("expires_in"),
            "tenant_id": tenant_id,
            "connected_at": int(time.time()),
        }
        store_tokens_in_db(user_id, teams_token_data, "teams")

        try:
            from services.memory.teams_memory import seed_teams_memory
            seed_teams_memory(user_id)
        except Exception:
            logger.warning("Failed to seed Teams memory (non-fatal)", exc_info=True)

        try:
            from routes.teams.teams_channel_metadata import auto_register_channels_task
            auto_register_channels_task.delay(user_id)
        except Exception:
            logger.warning("Failed to enqueue Teams channel registration (non-fatal)", exc_info=True)

        return redirect(f"{FRONTEND_URL}/teams/manage?teams_auth=success")
    except Exception:
        logger.exception("Error during Teams callback")
        return redirect(f"{FRONTEND_URL}?teams_auth=failed&error=unexpected_error")


def _tenant_from_graph(access_token: str) -> str | None:
    try:
        resp = requests.get(
            "https://graph.microsoft.com/v1.0/organization",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=20,
        )
        if resp.ok:
            values = resp.json().get("value") or []
            if values:
                return values[0].get("id")
    except Exception:
        logger.debug("Could not resolve tenant from Graph", exc_info=True)
    return None
