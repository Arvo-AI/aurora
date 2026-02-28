"""BigPanda integration routes.

Handles API-key connection, webhook ingestion, status, and disconnect.
"""

import hashlib
import hmac
import logging
import os
import secrets

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
    except Exception:
        logger.exception("Failed to retrieve BigPanda credentials for user %s", user_id)
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
        return jsonify({"error": "Failed to validate BigPanda token"}), 502

    webhook_secret = secrets.token_hex(32)
    try:
        store_tokens_in_db(
            user_id,
            {
                "api_token": api_token,
                "environment_count": validation.get("environment_count", 0),
                "webhook_secret": webhook_secret,
            },
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


def _verify_webhook_user(user_id: str) -> bool:
    """Verify the user_id has BigPanda credentials stored."""
    if not user_id or len(user_id) > 255:
        return False
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT 1 FROM user_tokens WHERE user_id = %s AND provider = %s LIMIT 1",
                    (user_id, "bigpanda"),
                )
                return cursor.fetchone() is not None
    except Exception as e:
        logger.warning("[BIGPANDA] Webhook user verification failed: %s", e)
        return False


@bigpanda_bp.route("/webhook/<user_id>", methods=["POST", "OPTIONS"])
def webhook(user_id: str):
    if request.method == "OPTIONS":
        return create_cors_response()

    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    if not _verify_webhook_user(user_id):
        logger.warning("[BIGPANDA] Webhook rejected: invalid or unconfigured user_id %s", user_id[:50])
        return jsonify({"error": "Invalid webhook configuration"}), 403

    creds = get_token_data(user_id, "bigpanda")
    if not creds:
        logger.warning("[BIGPANDA] Webhook received for user %s with no connection", user_id)
        return jsonify({"error": "BigPanda not connected for this user"}), 404

    webhook_secret = creds.get("webhook_secret")
    signature = request.headers.get("X-Aurora-Signature", "")

    if webhook_secret:
        if not signature:
            logger.warning("[BIGPANDA] Webhook rejected: missing X-Aurora-Signature for user %s", user_id[:50])
            return jsonify({"error": "Missing X-Aurora-Signature header"}), 401
        expected = hmac.new(webhook_secret.encode(), request.get_data(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            logger.warning("[BIGPANDA] Webhook rejected: invalid signature for user %s", user_id[:50])
            return jsonify({"error": "Invalid webhook signature"}), 401

    payload = request.get_json(silent=True) or {}
    logger.info("[BIGPANDA] Received webhook for user %s", user_id)

    _REDACTED_HEADERS = {"authorization", "cookie", "set-cookie", "proxy-authorization", "x-api-key"}
    sanitized_headers = {
        k: ("<REDACTED>" if k.lower() in _REDACTED_HEADERS or "token" in k.lower() or "secret" in k.lower() else v)
        for k, v in request.headers
    }

    try:
        from routes.bigpanda.tasks import process_bigpanda_event
        process_bigpanda_event.delay(payload, {"headers": sanitized_headers, "remote_addr": request.remote_addr}, user_id)
        return jsonify({"received": True})
    except Exception:
        logger.exception("[BIGPANDA] Failed to enqueue webhook event for user %s", user_id)
        return jsonify({"error": "Failed to process webhook"}), 503


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
    if not base_url:
        base_url = request.host_url.rstrip("/")

    return jsonify({
        "webhookUrl": f"{base_url}/bigpanda/webhook/{user_id}",
        "signatureHeader": "X-Aurora-Signature",
        "signatureAlgorithm": "HMAC-SHA256 of request body using your webhook secret",
        "instructions": [
            "1. In BigPanda, go to Integrations > Outbound Integrations > Webhooks",
            "2. Click 'New Integration' and select 'Alerts Webhook'",
            "3. Paste the webhook URL above",
            "4. Configure the signature header (X-Aurora-Signature) with HMAC-SHA256 of the request body",
            "5. Select which environments/incident types should trigger notifications",
            "6. Save the integration",
        ],
    })
