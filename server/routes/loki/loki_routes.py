import logging
import re
from typing import Any, Dict, Optional

from flask import Blueprint, jsonify, request

from connectors.loki_connector.client import LokiClient, LokiAPIError
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.secrets.secret_ref_utils import delete_user_secret
from utils.auth.rbac_decorators import require_permission
from utils.logging.secure_logging import mask_credential_value

logger = logging.getLogger(__name__)

loki_bp = Blueprint("loki", __name__)


def _normalize_base_url(raw_url: str) -> Optional[str]:
    """Normalize and validate a Loki base URL.

    Accepts both ``http://`` and ``https://`` schemes (Loki is often
    deployed on an internal network behind plain HTTP).
    """
    if not raw_url:
        return None

    url = raw_url.strip()
    if not url:
        return None

    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url

    url = url.rstrip("/")

    if not re.match(r"^https?://[A-Za-z0-9._:-]+(\/.*)?$", url):
        return None

    return url


def _get_stored_loki_credentials(user_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve stored Loki credentials from Vault for the given user."""
    try:
        return get_token_data(user_id, "loki")
    except Exception as exc:
        logger.error(f"Failed to retrieve Loki credentials for user {user_id}: {exc}")
        return None


@loki_bp.route("/connect", methods=["POST", "OPTIONS"])
@require_permission("connectors", "write")
def connect(user_id):
    """Validate Loki credentials and store them in Vault."""
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    # --- Validate base URL ---
    raw_base_url = data.get("baseUrl")
    base_url = _normalize_base_url(raw_base_url) if raw_base_url else None
    if not base_url:
        return jsonify({
            "error": "A valid Loki base URL is required (e.g., https://loki.example.com:3100)"
        }), 400

    # --- Validate auth type ---
    auth_type = (data.get("authType") or "none").strip().lower()
    if auth_type not in ("bearer", "basic", "none"):
        return jsonify({"error": "authType must be one of: bearer, basic, none"}), 400

    # --- Extract credentials based on auth type ---
    token = None
    username = None
    password = None

    if auth_type == "bearer":
        token = (data.get("token") or "").strip()
        if not token:
            return jsonify({"error": "API token is required for Bearer authentication"}), 400

    if auth_type == "basic":
        username = (data.get("username") or "").strip()
        password = (data.get("password") or "").strip()
        if not username or not password:
            return jsonify({
                "error": "Username and password are required for Basic authentication"
            }), 400

    tenant_id = (data.get("tenantId") or "").strip() or None

    # --- Validate connection ---
    logger.info(f"[LOKI] Connecting user {user_id} to {base_url} (auth={auth_type})")
    if token:
        logger.info(f"[LOKI] Token: {mask_credential_value(token)}")

    client = LokiClient(
        base_url,
        auth_type=auth_type,
        token=token,
        username=username,
        password=password,
        tenant_id=tenant_id,
    )

    try:
        result = client.test_connection()
    except LokiAPIError as exc:
        logger.error(f"[LOKI] Connection validation failed for user {user_id}: {exc}")
        return jsonify({"error": f"Failed to validate Loki connection: {exc}"}), 502

    labels_count = len(result.get("labels", []))

    # --- Store credentials in Vault ---
    token_payload = {
        "auth_type": auth_type,
        "base_url": base_url,
        "tenant_id": tenant_id,
    }
    if auth_type == "bearer":
        token_payload["token"] = token
    elif auth_type == "basic":
        token_payload["username"] = username
        token_payload["password"] = password

    try:
        store_tokens_in_db(user_id, token_payload, "loki")
        logger.info(
            f"[LOKI] Stored credentials for user {user_id} "
            f"(auth={auth_type}, labels={labels_count})"
        )
    except Exception as exc:
        logger.exception(f"[LOKI] Failed to store credentials for user {user_id}: {exc}")
        return jsonify({"error": "Failed to store Loki credentials"}), 500

    return jsonify({
        "success": True,
        "baseUrl": base_url,
        "authType": auth_type,
        "tenantId": tenant_id,
        "labelsCount": labels_count,
    })


@loki_bp.route("/status", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def status(user_id):
    """Return Loki connection metadata for the authenticated user."""
    creds = _get_stored_loki_credentials(user_id)
    if not creds:
        return jsonify({"connected": False})

    base_url = creds.get("base_url")
    auth_type = creds.get("auth_type", "none")

    if not base_url:
        return jsonify({"connected": False})

    return jsonify({
        "connected": True,
        "baseUrl": base_url,
        "authType": auth_type,
        "tenantId": creds.get("tenant_id"),
    })


@loki_bp.route("/disconnect", methods=["POST", "DELETE", "OPTIONS"])
@require_permission("connectors", "write")
def disconnect(user_id):
    """Disconnect Loki by removing stored credentials from Vault."""
    try:
        success, deleted_count = delete_user_secret(user_id, "loki")
        if not success:
            logger.warning("[LOKI] Failed to clean up secrets during disconnect")
            return jsonify({
                "success": False,
                "error": "Failed to delete stored credentials"
            }), 500

        logger.info("[LOKI] Disconnected provider (deleted %s token entries)", deleted_count)

        return jsonify({
            "success": True,
            "message": "Loki disconnected successfully",
            "deleted": deleted_count,
        }), 200

    except Exception as exc:
        logger.exception("[LOKI] Failed to disconnect provider")
        return jsonify({"error": "Failed to disconnect Loki"}), 500
