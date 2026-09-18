"""Splunk On-Call connector routes."""

import hmac
import logging
import os
import secrets
from typing import Any

from flask import Blueprint, jsonify, request

from routes.splunk_on_call.helpers import SplunkOnCallAPIError, SplunkOnCallClient
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import validate_user_exists
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.log_sanitizer import sanitize
from utils.secrets.secret_ref_utils import delete_user_secret

logger = logging.getLogger(__name__)
splunk_on_call_bp = Blueprint("splunk_on_call", __name__)


def _get_stored_credentials(user_id: str) -> dict[str, Any]:
    try:
        return get_token_data(user_id, "splunk_on_call") or {}
    except Exception:
        logger.exception(
            "[SPLUNK_ON_CALL] Failed to retrieve credentials for user %s",
            sanitize(user_id),
        )
        return {}


@splunk_on_call_bp.route("/connect", methods=["POST"])
@require_permission("connectors", "write")
def connect(user_id: str):
    data = request.get_json(silent=True) or {}
    api_id = str(data.get("apiId") or "").strip()
    api_key = str(data.get("apiKey") or "").strip()
    routing_key_contains = str(data.get("routingKeyContains") or "").strip()
    if not api_id or not api_key:
        return jsonify({"error": "apiId and apiKey are required"}), 400

    client = SplunkOnCallClient(api_id, api_key)
    try:
        incident_count = client.validate_credentials()
    except SplunkOnCallAPIError as exc:
        if exc.status_code in (401, 403):
            return jsonify({"error": "Splunk On-Call credentials were rejected"}), 401
        logger.warning(
            "[SPLUNK_ON_CALL] Credential validation failed with status %s",
            exc.status_code,
        )
        return jsonify({"error": "Splunk On-Call credential validation failed"}), 502

    webhook_secret = secrets.token_urlsafe(32)
    try:
        store_tokens_in_db(
            user_id,
            {
                "api_id": api_id,
                "api_key": api_key,
                "routing_key_contains": routing_key_contains,
                "webhook_secret": webhook_secret,
            },
            "splunk_on_call",
        )
    except Exception:
        logger.exception("[SPLUNK_ON_CALL] Failed to store credentials")
        return jsonify({"error": "Failed to store Splunk On-Call credentials"}), 500

    return jsonify({
        "success": True,
        "connected": True,
        "incidentCount": incident_count,
        "routingKeyContains": routing_key_contains,
        "webhookSecret": webhook_secret,
    })


@splunk_on_call_bp.route("/status", methods=["GET"])
@require_permission("connectors", "read")
def status(user_id: str):
    creds = _get_stored_credentials(user_id)
    if not creds.get("api_id") or not creds.get("api_key"):
        return jsonify({"connected": False})
    return jsonify({
        "connected": True,
        "routingKeyContains": creds.get("routing_key_contains") or "",
    })


@splunk_on_call_bp.route("/disconnect", methods=["POST", "DELETE"])
@require_permission("connectors", "write")
def disconnect(user_id: str):
    try:
        success, deleted = delete_user_secret(user_id, "splunk_on_call")
        if not success:
            return jsonify({"error": "Failed to delete Splunk On-Call credentials"}), 500
        return jsonify({"success": True, "deleted": deleted})
    except Exception:
        logger.exception("[SPLUNK_ON_CALL] Disconnect failed")
        return jsonify({"error": "Failed to disconnect Splunk On-Call"}), 500


@splunk_on_call_bp.route("/webhook-url", methods=["GET"])
@require_permission("connectors", "read")
def webhook_url(user_id: str):
    creds = _get_stored_credentials(user_id)
    if not creds.get("api_id"):
        return jsonify({"error": "Splunk On-Call is not connected"}), 404

    ngrok_url = os.getenv("NGROK_URL", "").rstrip("/")
    backend_url = os.getenv("NEXT_PUBLIC_BACKEND_URL", "").rstrip("/")
    base_url = ngrok_url if ngrok_url and backend_url.startswith("http://localhost") else backend_url
    if not base_url:
        base_url = request.host_url.rstrip("/")

    return jsonify({
        "webhookUrl": f"{base_url}/splunk-on-call/webhook/{user_id}",
        "webhookSecret": creds.get("webhook_secret"),
        "secretHeader": "X-Aurora-Webhook-Secret",
        "instructions": [
            "Create an Any-Incident outgoing webhook in Splunk On-Call.",
            "Paste the webhook URL and add the authentication header.",
            "Use POST and application/json. Send the native incident body with all available variables.",
        ],
    })


@splunk_on_call_bp.route("/webhook/<user_id>", methods=["POST"])
def webhook(user_id: str):
    if not user_id or len(user_id) > 255 or not validate_user_exists(user_id):
        return jsonify({"error": "Invalid webhook configuration"}), 403

    creds = _get_stored_credentials(user_id)
    expected = str(creds.get("webhook_secret") or "")
    supplied = request.headers.get("X-Aurora-Webhook-Secret", "")
    if (
        not expected
        or len(expected) != len(supplied)
        or not hmac.compare_digest(expected, supplied)
    ):
        logger.warning(
            "[SPLUNK_ON_CALL] Rejected unauthenticated webhook for user %s",
            sanitize(user_id),
        )
        return jsonify({"error": "Invalid webhook secret"}), 401

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "Payload must be a JSON object"}), 400

    try:
        from routes.splunk_on_call.tasks import process_splunk_on_call_event

        process_splunk_on_call_event.delay(payload, user_id)
        return jsonify({"received": True}), 202
    except Exception:
        logger.exception(
            "[SPLUNK_ON_CALL] Failed to enqueue webhook event for user %s",
            sanitize(user_id),
        )
        return jsonify({"error": "Failed to process webhook"}), 503
