"""BigPanda integration routes.

Handles API-key connection, webhook ingestion, status, and disconnect.
"""

import json
import logging
import os

from flask import Blueprint, jsonify, request

from connectors.bigpanda_connector.api_client import BigPandaClient, BigPandaAPIError
from utils.db.connection_pool import db_pool
from utils.web.cors_utils import create_cors_response
from utils.auth.stateless_auth import get_user_id_from_request
from utils.auth.token_management import get_token_data, store_tokens_in_db

logger = logging.getLogger(__name__)

bigpanda_bp = Blueprint("bigpanda", __name__)


def _get_stored_credentials(user_id: str) -> dict | None:
    try:
        return get_token_data(user_id, "bigpanda")
    except Exception as exc:
        logger.error("Failed to retrieve BigPanda credentials for user %s: %s", user_id, exc)
        return None


@bigpanda_bp.route("/connect", methods=["POST", "OPTIONS"])
def connect():
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    data = request.get_json(force=True, silent=True) or {}
    api_token = data.get("apiToken")

    if not api_token or not isinstance(api_token, str):
        return jsonify({"error": "apiToken is required"}), 400

    logger.info("[BIGPANDA] Connecting user %s", user_id)

    client = BigPandaClient(api_token)
    try:
        validation = client.validate_token()
    except BigPandaAPIError as exc:
        logger.error("[BIGPANDA] Token validation failed for user %s: %s", user_id, exc)
        return jsonify({"error": str(exc)}), 502

    try:
        store_tokens_in_db(
            user_id,
            {"api_token": api_token, "environment_count": validation.get("environment_count", 0)},
            "bigpanda",
        )
    except Exception as exc:
        logger.exception("[BIGPANDA] Failed to store credentials for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to store BigPanda credentials"}), 500

    return jsonify({"success": True, "connected": True, "environmentCount": validation.get("environment_count", 0)})


@bigpanda_bp.route("/status", methods=["GET", "OPTIONS"])
def status():
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    creds = _get_stored_credentials(user_id)
    if not creds or not creds.get("api_token"):
        return jsonify({"connected": False})

    return jsonify({
        "connected": True,
        "environmentCount": creds.get("environment_count"),
    })


@bigpanda_bp.route("/disconnect", methods=["POST", "DELETE", "OPTIONS"])
def disconnect():
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    try:
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM user_tokens WHERE user_id = %s AND provider = %s",
                (user_id, "bigpanda"),
            )
            conn.commit()
        return jsonify({"success": True, "message": "BigPanda disconnected successfully"})
    except Exception as exc:
        logger.exception("[BIGPANDA] Failed to disconnect user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to disconnect BigPanda"}), 500


@bigpanda_bp.route("/webhook/<user_id>", methods=["POST", "OPTIONS"])
def webhook(user_id: str):
    if request.method == "OPTIONS":
        return create_cors_response()

    try:
        creds = get_token_data(user_id, "bigpanda")
    except Exception as exc:
        logger.error("[BIGPANDA] Failed to retrieve credentials for webhook user %s: %s", user_id, exc)
        return jsonify({"error": "Internal error processing webhook"}), 500
    if not creds:
        logger.warning("[BIGPANDA] Webhook received for user %s with no connection", user_id)
        return jsonify({"error": "BigPanda not connected for this user"}), 404

    payload = request.get_json(silent=True) or {}
    logger.info("[BIGPANDA] Received webhook for user %s", user_id)

    _REDACTED_HEADERS = {"authorization", "cookie", "set-cookie", "proxy-authorization", "x-api-key"}
    sanitized_headers = {
        k: ("<REDACTED>" if k.lower() in _REDACTED_HEADERS or "token" in k.lower() or "secret" in k.lower() else v)
        for k, v in request.headers
    }

    from routes.bigpanda.tasks import process_bigpanda_event
    process_bigpanda_event.delay(payload, {"headers": sanitized_headers, "remote_addr": request.remote_addr}, user_id)
    return jsonify({"received": True})


@bigpanda_bp.route("/webhook-url", methods=["GET", "OPTIONS"])
def get_webhook_url():
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    ngrok_url = os.getenv("NGROK_URL", "").rstrip("/")
    backend_url = os.getenv("NEXT_PUBLIC_BACKEND_URL", "").rstrip("/")
    base_url = ngrok_url if ngrok_url and backend_url.startswith("http://localhost") else backend_url

    return jsonify({
        "webhookUrl": f"{base_url}/bigpanda/webhook/{user_id}",
        "instructions": [
            "1. In BigPanda, go to Integrations > Outbound Integrations > Webhooks",
            "2. Click 'New Integration' and select 'Alerts Webhook'",
            "3. Paste the webhook URL above",
            "4. Select which environments/incident types should trigger notifications",
            "5. Save the integration",
        ],
    })
