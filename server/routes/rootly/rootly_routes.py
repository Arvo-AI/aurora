"""Rootly integration routes.

Handles API-key connection, webhook ingestion, status, and disconnect.
Rootly webhooks support: incident.created, incident.updated,
incident.mitigated, incident.resolved, incident.cancelled.
"""

import hashlib
import hmac
import json
import logging
import os

from flask import Blueprint, jsonify, request

from connectors.rootly_connector.api_client import RootlyClient, RootlyAPIError
from utils.db.connection_pool import db_pool
from utils.web.cors_utils import create_cors_response
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.auth.rbac_decorators import require_permission

logger = logging.getLogger(__name__)

rootly_bp = Blueprint("rootly", __name__)


def _get_stored_credentials(user_id: str) -> dict | None:
    try:
        return get_token_data(user_id, "rootly")
    except Exception:
        logger.exception("Failed to retrieve Rootly credentials for user %s", user_id)
        return None


@rootly_bp.route("/connect", methods=["POST", "OPTIONS"])
@require_permission("connectors", "write")
def connect(user_id):
    """Connect Rootly with an API token."""
    data = request.get_json(force=True, silent=True) or {}
    api_token = data.get("apiToken")

    if not api_token or not isinstance(api_token, str):
        return jsonify({"error": "apiToken is required"}), 400

    api_token = api_token.strip()
    logger.info("[ROOTLY] Connecting user %s", user_id)

    client = RootlyClient(api_token)
    try:
        validation = client.validate_token()
    except RootlyAPIError as exc:
        logger.error("[ROOTLY] Token validation failed for user %s: %s", user_id, exc)
        return jsonify({"error": str(exc)}), 502

    user_info = {}
    try:
        user_info = client.get_current_user()
    except RootlyAPIError:
        pass

    try:
        store_tokens_in_db(
            user_id,
            {
                "api_token": api_token,
                "authorization_count": validation.get("authorization_count", 0),
                "user_email": user_info.get("email"),
                "user_name": user_info.get("name"),
            },
            "rootly",
        )
    except Exception as exc:
        logger.exception("[ROOTLY] Failed to store credentials for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to store Rootly credentials"}), 500

    return jsonify({
        "success": True,
        "connected": True,
        "userEmail": user_info.get("email"),
        "userName": user_info.get("name"),
    })


@rootly_bp.route("/status", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def status(user_id):
    """Get Rootly connection status."""
    creds = _get_stored_credentials(user_id)
    if not creds or not creds.get("api_token"):
        return jsonify({"connected": False})

    return jsonify({
        "connected": True,
        "userEmail": creds.get("user_email"),
        "userName": creds.get("user_name"),
    })


@rootly_bp.route("/disconnect", methods=["POST", "DELETE", "OPTIONS"])
@require_permission("connectors", "write")
def disconnect(user_id):
    """Disconnect Rootly."""
    try:
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM user_tokens WHERE user_id = %s AND provider = %s",
                (user_id, "rootly"),
            )
            conn.commit()
        return jsonify({"success": True, "message": "Rootly disconnected successfully"})
    except Exception as exc:
        logger.exception("[ROOTLY] Failed to disconnect user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to disconnect Rootly"}), 500


def _verify_webhook_signature(payload_body: bytes, signature_header: str, secret: str) -> bool:
    """Verify Rootly webhook signature (HMAC-SHA256).

    Rootly sends X-Rootly-Signature with format: t=<timestamp>,v1=<signature>
    The signature is SHA256 HMAC of "<timestamp>.<body>".
    """
    if not signature_header or not secret:
        return False

    parts = {}
    for part in signature_header.split(","):
        key_val = part.strip().split("=", 1)
        if len(key_val) == 2:
            parts[key_val[0]] = key_val[1]

    timestamp = parts.get("t")
    sig = parts.get("v1")

    if not timestamp or not sig:
        return False

    signed_payload = f"{timestamp}.{payload_body.decode('utf-8')}"
    expected = hmac.new(secret.encode(), signed_payload.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


@rootly_bp.route("/webhook/<user_id>", methods=["POST", "OPTIONS"])
def webhook(user_id: str):
    """Receive webhook events from Rootly."""
    if request.method == "OPTIONS":
        return create_cors_response()

    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    creds = get_token_data(user_id, "rootly")
    if not creds:
        logger.warning("[ROOTLY] Webhook received for user %s with no Rootly connection", user_id)
        return jsonify({"error": "Rootly not connected for this user"}), 404

    payload = request.get_json(silent=True) or {}
    logger.info("[ROOTLY] Raw webhook payload for user %s: %s", user_id, json.dumps(payload)[:500])

    event = payload.get("event", {})
    event_type = event.get("type", "")

    if not event_type:
        logger.warning("[ROOTLY] Missing event type in webhook for user %s", user_id)
        return jsonify({"error": "Missing event type"}), 400

    MONITORED_EVENTS = {
        "incident.created",
        "incident.updated",
        "incident.mitigated",
        "incident.resolved",
        "incident.cancelled",
    }

    if event_type not in MONITORED_EVENTS:
        logger.debug("[ROOTLY] Ignoring event type: %s", event_type)
        return jsonify({"received": True, "reason": "event type not monitored"})

    try:
        from routes.rootly.tasks import process_rootly_event

        _REDACTED_HEADERS = {"authorization", "cookie", "set-cookie", "proxy-authorization", "x-api-key"}
        sanitized_headers = {
            k: ("<REDACTED>" if k.lower() in _REDACTED_HEADERS or "token" in k.lower() or "secret" in k.lower() else v)
            for k, v in request.headers
        }

        process_rootly_event.delay(
            raw_payload=payload,
            metadata={"headers": sanitized_headers, "remote_addr": request.remote_addr},
            user_id=user_id,
        )
        logger.info("[ROOTLY] Enqueued event for processing: user=%s, type=%s", user_id, event_type)
        return jsonify({"received": True})
    except Exception:
        logger.exception("[ROOTLY] Failed to enqueue webhook event for user %s", user_id)
        return jsonify({"error": "Failed to process webhook"}), 503


@rootly_bp.route("/webhook-url", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def get_webhook_url(user_id):
    """Get the webhook URL that should be configured in Rootly."""
    ngrok_url = os.getenv("NGROK_URL", "").rstrip("/")
    backend_url = os.getenv("NEXT_PUBLIC_BACKEND_URL", "").rstrip("/")
    base_url = ngrok_url if ngrok_url and backend_url.startswith("http://localhost") else backend_url
    if not base_url:
        base_url = request.host_url.rstrip("/")

    return jsonify({
        "webhookUrl": f"{base_url}/rootly/webhook/{user_id}",
        "instructions": [
            "1. In Rootly, go to Settings > Webhooks",
            "2. Click 'New Webhook Endpoint'",
            "3. Paste the webhook URL above",
            "4. Select incident events: incident.created, incident.updated, incident.mitigated, incident.resolved",
            "5. Save the webhook configuration",
        ],
    })
