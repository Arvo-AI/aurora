import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from flask import Blueprint, jsonify, request

from connectors.thousandeyes_connector.client import (
    ThousandEyesAPIError,
    get_thousandeyes_client,
    invalidate_thousandeyes_client,
)
from chat.backend.agent.tools.mcp_tools import clear_credentials_cache
from utils.auth.stateless_auth import get_user_id_from_request
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.secrets.secret_ref_utils import delete_user_secret
from utils.web.cors_utils import create_cors_response

logger = logging.getLogger(__name__)

thousandeyes_bp = Blueprint("thousandeyes", __name__)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _get_stored_credentials(user_id: str) -> Optional[Dict[str, Any]]:
    try:
        data = get_token_data(user_id, "thousandeyes")
        return data or None
    except Exception as exc:
        logger.error("[THOUSANDEYES] Failed to retrieve credentials for user %s: %s", user_id, exc)
        return None


# ------------------------------------------------------------------
# Connection management
# ------------------------------------------------------------------

@thousandeyes_bp.route("/connect", methods=["POST", "OPTIONS"])
def connect():
    if request.method == "OPTIONS":
        return create_cors_response()

    payload = request.get_json(force=True, silent=True) or {}

    user_id = get_user_id_from_request()
    api_token = payload.get("api_token", "").strip()
    account_group_id = payload.get("account_group_id", "").strip() or None

    if not user_id:
        return jsonify({"error": "User authentication required"}), 401
    if not api_token:
        return jsonify({"error": "Bearer token is required"}), 400

    logger.info("[THOUSANDEYES] Connecting user %s", user_id)

    try:
        client = get_thousandeyes_client(
            user_id, api_token=api_token, account_group_id=account_group_id
        )
        account_groups = client.get_account_status().get("accountGroups", [])
    except ThousandEyesAPIError as exc:
        logger.warning("[THOUSANDEYES] Validation failed for user %s: %s", user_id, exc)
        return jsonify({"error": str(exc)}), 400

    token_payload = {
        "api_token": api_token,
        "account_group_id": account_group_id,
        "account_groups": account_groups,
        "validated_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        store_tokens_in_db(user_id, token_payload, "thousandeyes")
        logger.info("[THOUSANDEYES] Stored credentials for user %s", user_id)
    except Exception as exc:
        logger.exception("[THOUSANDEYES] Failed to store credentials: %s", exc)
        return jsonify({"error": "Failed to store ThousandEyes credentials"}), 500

    clear_credentials_cache(user_id)

    return jsonify({
        "success": True,
        "account_groups": account_groups,
    })


@thousandeyes_bp.route("/status", methods=["GET", "OPTIONS"])
def status():
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    creds = _get_stored_credentials(user_id)
    if not creds:
        return jsonify({"connected": False})

    api_token = creds.get("api_token")
    if not api_token:
        return jsonify({"connected": False})

    account_group_id = creds.get("account_group_id")

    try:
        client = get_thousandeyes_client(
            user_id, api_token=api_token, account_group_id=account_group_id
        )
        account_groups = client.get_account_status().get("accountGroups", [])
    except ThousandEyesAPIError as exc:
        logger.warning("[THOUSANDEYES] Status validation failed for user %s: %s", user_id, exc)
        return jsonify({
            "connected": False,
            "error": "Failed to validate ThousandEyes connection",
        })

    return jsonify({
        "connected": True,
        "account_group_id": account_group_id,
        "account_groups": account_groups,
        "validatedAt": creds.get("validated_at"),
    })


@thousandeyes_bp.route("/disconnect", methods=["DELETE", "POST", "OPTIONS"])
def disconnect():
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    try:
        invalidate_thousandeyes_client(user_id)
        vault_ok, rows = delete_user_secret(user_id, "thousandeyes")

        if not vault_ok:
            logger.warning("[THOUSANDEYES] Disconnected user %s but Vault delete failed", user_id)

        clear_credentials_cache(user_id)

        logger.info("[THOUSANDEYES] Disconnected user %s", user_id)
        return jsonify({
            "success": True,
            "message": "ThousandEyes disconnected successfully",
            "tokensDeleted": rows,
        })
    except Exception as exc:
        logger.exception("[THOUSANDEYES] Failed to disconnect user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to disconnect ThousandEyes"}), 500
