import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from flask import Blueprint, jsonify, request

from connectors.coroot_connector.client import CorootAPIError, CorootClient
from utils.auth.stateless_auth import get_user_id_from_request
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.secrets.secret_ref_utils import delete_user_secret
from utils.web.cors_utils import create_cors_response

logger = logging.getLogger(__name__)

coroot_bp = Blueprint("coroot", __name__)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _get_stored_coroot_credentials(user_id: str) -> Optional[Dict[str, Any]]:
    try:
        data = get_token_data(user_id, "coroot")
        return data or None
    except Exception as exc:
        logger.error("[COROOT] Failed to retrieve credentials for user %s: %s", user_id, exc)
        return None


def _build_client_from_creds(creds: Dict[str, Any]) -> Optional[CorootClient]:
    url = creds.get("url")
    email = creds.get("email")
    password = creds.get("password")
    session_cookie = creds.get("session_cookie")
    if not url or not email or not password:
        return None
    return CorootClient(
        url=url, email=email, password=password, session_cookie=session_cookie,
    )


# ------------------------------------------------------------------
# Connection management
# ------------------------------------------------------------------

@coroot_bp.route("/connect", methods=["POST", "OPTIONS"])
def connect():
    if request.method == "OPTIONS":
        return create_cors_response()

    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}

    user_id = get_user_id_from_request()
    url = payload.get("url", "").strip()
    email = payload.get("email", "").strip()
    password = payload.get("password", "")

    if not user_id:
        return jsonify({"error": "User authentication required"}), 401
    if not url:
        return jsonify({"error": "Coroot URL is required"}), 400
    if not email:
        return jsonify({"error": "Email is required"}), 400
    if not password:
        return jsonify({"error": "Password is required"}), 400

    logger.info("[COROOT] Connecting user %s to %s", user_id, url)

    client = CorootClient(url=url, email=email, password=password)

    try:
        client.login()
    except CorootAPIError as exc:
        logger.warning("[COROOT] Login failed for user %s: %s", user_id, exc)
        msg = str(exc)
        safe_messages = {
            "Invalid email or password",
            "Unable to reach Coroot server",
            "Login succeeded but no session cookie was returned",
        }
        if msg not in safe_messages:
            msg = "Failed to connect to Coroot"
        return jsonify({"error": msg}), 400

    try:
        projects = client.discover_projects()
    except CorootAPIError as exc:
        logger.warning("[COROOT] Credential valid but project discovery failed: %s", exc)
        projects = []

    token_payload = {
        "url": url,
        "email": email,
        "password": password,
        "session_cookie": client.session_cookie,
        "projects": projects,
        "validated_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        store_tokens_in_db(user_id, token_payload, "coroot")
        logger.info("[COROOT] Stored credentials for user %s (url=%s)", user_id, url)
    except Exception as exc:
        logger.exception("[COROOT] Failed to store credentials: %s", exc)
        return jsonify({"error": "Failed to store Coroot credentials"}), 500

    return jsonify({
        "success": True,
        "url": url,
        "projects": projects,
    })


@coroot_bp.route("/status", methods=["GET", "OPTIONS"])
def status():
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    creds = _get_stored_coroot_credentials(user_id)
    if not creds:
        return jsonify({"connected": False})

    client = _build_client_from_creds(creds)
    if not client:
        return jsonify({"connected": False})

    try:
        projects = client.discover_projects()
    except CorootAPIError as exc:
        logger.warning("[COROOT] Status validation failed for user %s: %s", user_id, exc)
        return jsonify({
            "connected": False,
            "error": "Failed to validate Coroot connection",
        })

    return jsonify({
        "connected": True,
        "url": creds.get("url"),
        "email": creds.get("email"),
        "projects": projects,
        "validatedAt": creds.get("validated_at"),
    })


@coroot_bp.route("/disconnect", methods=["DELETE", "POST", "OPTIONS"])
def disconnect():
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    try:
        vault_ok, rows = delete_user_secret(user_id, "coroot")

        if not vault_ok:
            logger.warning("[COROOT] Disconnected user %s but Vault delete failed", user_id)

        logger.info("[COROOT] Disconnected user %s", user_id)
        return jsonify({
            "success": True,
            "message": "Coroot disconnected successfully",
            "tokensDeleted": rows,
        })
    except Exception as exc:
        logger.exception("[COROOT] Failed to disconnect user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to disconnect Coroot"}), 500
