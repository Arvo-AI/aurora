import base64
import logging
import re
from typing import Any, Dict, Optional

import requests
from flask import Blueprint, jsonify, request

from utils.logging.secure_logging import mask_credential_value
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.auth.rbac_decorators import require_permission
from utils.secrets.secret_ref_utils import delete_user_secret

LOKI_TIMEOUT = 15

logger = logging.getLogger(__name__)

loki_bp = Blueprint("loki", __name__)


class LokiAPIError(Exception):
    """Custom error for Loki API interactions."""


class LokiClient:
    def __init__(self, base_url: str, api_token: str, username: Optional[str] = None):
        self.base_url = base_url
        self.api_token = api_token
        self.username = username

    @staticmethod
    def normalize_base_url(raw_url: str) -> Optional[str]:
        if not raw_url:
            return None

        url = raw_url.strip()
        if not url:
            return None

        if not re.match(r"^https?://", url, re.IGNORECASE):
            url = "https://" + url

        url = url.rstrip("/")

        if not re.match(r"^https?://[A-Za-z0-9._-]+(:[0-9]{2,5})?(\/.*)?$", url):
            return None

        return url

    @property
    def headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        if self.username:
            encoded = base64.b64encode(
                f"{self.username}:{self.api_token}".encode()
            ).decode()
            headers["Authorization"] = f"Basic {encoded}"
        else:
            headers["Authorization"] = f"Bearer {self.api_token}"

        return headers

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.base_url}{path}"
        try:
            response = requests.request(
                method, url, headers=self.headers, timeout=LOKI_TIMEOUT, **kwargs
            )
            response.raise_for_status()
            return response
        except requests.HTTPError as exc:
            logger.error("[LOKI] %s %s failed: %s", method, url, exc)
            raise LokiAPIError(str(exc)) from exc
        except requests.RequestException as exc:
            logger.error("[LOKI] %s %s error: %s", method, url, exc)
            raise LokiAPIError("Unable to reach Loki") from exc

    def get_labels(self) -> Dict[str, Any]:
        """Fetch label names — lightweight call to validate connectivity."""
        return self._request("GET", "/loki/api/v1/labels").json()

    def get_ready(self) -> bool:
        """Check Loki readiness endpoint."""
        try:
            resp = self._request("GET", "/ready")
            return resp.status_code == 200
        except LokiAPIError:
            return False


def _get_stored_loki_credentials(user_id: str) -> Optional[Dict[str, Any]]:
    try:
        return get_token_data(user_id, "loki")
    except Exception as exc:
        logger.error("Failed to retrieve Loki credentials for user %s: %s", user_id, exc)
        return None


@loki_bp.route("/connect", methods=["POST", "OPTIONS"])
@require_permission("connectors", "write")
def connect(user_id):
    """Store Loki credentials and validate connectivity."""
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    api_token = (data.get("apiToken") or data.get("token") or "").strip()
    raw_base_url = data.get("baseUrl")
    username = (data.get("username") or "").strip() or None

    if not api_token or not isinstance(api_token, str):
        return jsonify({"error": "Loki API token is required"}), 400

    base_url = LokiClient.normalize_base_url(raw_base_url) if raw_base_url else None
    if not base_url:
        return jsonify({"error": "A valid Loki URL is required (e.g. https://logs-prod.grafana.net)"}), 400

    masked_token = mask_credential_value(api_token)
    logger.info("[LOKI] Connecting user %s to %s (token=%s)", user_id, base_url, masked_token)

    client = LokiClient(base_url, api_token, username)

    try:
        labels_resp = client.get_labels()
        if labels_resp.get("status") != "success":
            raise LokiAPIError("Unexpected response from Loki labels endpoint")
    except LokiAPIError as exc:
        logger.error("[LOKI] Connection validation failed for user %s: %s", user_id, exc)
        return jsonify({
            "error": "Failed to validate Loki credentials. Check URL, token, and permissions."
        }), 502

    label_count = len(labels_resp.get("data", []))

    token_payload = {
        "api_token": api_token,
        "base_url": base_url,
        "username": username,
        "label_count": label_count,
    }

    try:
        store_tokens_in_db(user_id, token_payload, "loki")
        logger.info("[LOKI] Stored credentials for user %s (labels=%d)", user_id, label_count)
    except Exception as exc:
        logger.exception("[LOKI] Failed to store credentials for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to store Loki credentials"}), 500

    return jsonify({
        "success": True,
        "baseUrl": base_url,
        "username": username,
        "labelCount": label_count,
    })


@loki_bp.route("/status", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def status(user_id):
    creds = _get_stored_loki_credentials(user_id)
    if not creds:
        return jsonify({"connected": False})

    api_token = creds.get("api_token")
    base_url = creds.get("base_url")

    if not api_token or not base_url:
        logger.warning("[LOKI] Incomplete credentials for user %s", user_id)
        return jsonify({"connected": False})

    return jsonify({
        "connected": True,
        "baseUrl": base_url,
        "username": creds.get("username"),
        "labelCount": creds.get("label_count"),
    })


@loki_bp.route("/disconnect", methods=["POST", "DELETE", "OPTIONS"])
@require_permission("connectors", "write")
def disconnect(user_id):
    """Disconnect Loki by removing stored credentials."""
    try:
        success, deleted_count = delete_user_secret(user_id, "loki")
        if not success:
            logger.warning("[LOKI] Failed to clean up secrets during disconnect")
            return jsonify({"success": False, "error": "Failed to delete stored credentials"}), 500

        logger.info("[LOKI] Disconnected provider (deleted %s token entries)", deleted_count)

        return jsonify({
            "success": True,
            "message": "Loki disconnected successfully",
            "deleted": deleted_count,
        }), 200

    except Exception:
        logger.exception("[LOKI] Failed to disconnect provider")
        return jsonify({"error": "Failed to disconnect Loki"}), 500
