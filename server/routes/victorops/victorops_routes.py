"""Splunk On-Call (VictorOps) integration routes."""

import json
import logging
import os

from flask import Blueprint, jsonify, request

from utils.auth.rbac_decorators import require_permission
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.log_sanitizer import sanitize
from utils.secrets.secret_ref_utils import delete_user_secret
from routes.victorops.victorops_helpers import (
    VictorOpsAPIError,
    VictorOpsClient,
    error_response,
    validate_credentials,
)

logger = logging.getLogger(__name__)
victorops_bp = Blueprint("victorops", __name__)


@victorops_bp.route("", methods=["GET"])
@require_permission("connectors", "read")
def victorops_status(user_id):
    """Get Splunk On-Call connection status."""
    creds = get_token_data(user_id, "victorops")
    if not creds:
        return jsonify({"connected": False})

    return jsonify({
        "connected": True,
        "displayName": creds.get("display_name", "Splunk On-Call"),
        "validatedAt": creds.get("validated_at"),
        "externalUserName": creds.get("external_user_name"),
        "accountName": creds.get("account_name"),
        "capabilities": creds.get("capabilities", {}),
    })


@victorops_bp.route("", methods=["POST"])
@require_permission("connectors", "write")
def victorops_connect(user_id):
    """Connect Splunk On-Call using API ID and API Key."""
    data = request.get_json(force=True, silent=True) or {}
    api_id = (data.get("apiId") or "").strip()
    api_key = (data.get("apiKey") or "").strip()
    display_name = data.get("displayName", "Splunk On-Call")

    if not api_id or not api_key:
        return jsonify({"error": "Both API ID and API Key are required"}), 400

    logger.info("[VICTOROPS] Validating credentials for user %s", user_id)
    try:
        client = VictorOpsClient(api_id=api_id, api_key=api_key)
        token_info = validate_credentials(client)
        logger.info("[VICTOROPS] Credentials validated for user %s", user_id)
    except VictorOpsAPIError as e:
        logger.warning("[VICTOROPS] Credential validation failed for user %s: %s", user_id, e)
        return error_response(e)

    token_data = {
        "api_id": api_id,
        "api_key": api_key,
        "display_name": display_name,
        **token_info,
    }

    try:
        store_tokens_in_db(user_id, token_data, "victorops")
    except Exception:
        logger.exception("[VICTOROPS] Failed to store credentials for user %s", user_id)
        return jsonify({"error": "Storage failed"}), 500

    return jsonify({"success": True, "connected": True, "displayName": display_name, **token_info})


@victorops_bp.route("", methods=["DELETE"])
@require_permission("connectors", "write")
def victorops_disconnect(user_id):
    """Disconnect Splunk On-Call."""
    try:
        success, deleted = delete_user_secret(user_id, "victorops")
        if not success:
            logger.warning("[VICTOROPS] Failed to clean up secrets during disconnect")
            return jsonify({"success": False, "error": "Failed to delete stored credentials"}), 500

        logger.info("[VICTOROPS] Disconnected (deleted %d token rows)", deleted)
        return jsonify({"success": True, "deleted": deleted})
    except Exception:
        logger.exception("[VICTOROPS] Disconnect failed")
        return jsonify({"error": "Disconnect failed"}), 500


@victorops_bp.route("/webhook-url", methods=["GET"])
@require_permission("connectors", "read")
def get_webhook_url(user_id):
    """Return the webhook URL to configure in Splunk On-Call."""
    ngrok_url = os.getenv("NGROK_URL", "").rstrip("/")
    backend_url = os.getenv("NEXT_PUBLIC_BACKEND_URL", "").rstrip("/")

    base_url = ngrok_url if (ngrok_url and backend_url.startswith("http://localhost")) else backend_url
    webhook_url = f"{base_url}/victorops/webhook/{user_id}"

    return jsonify({
        "webhookUrl": webhook_url,
        "instructions": [
            "1. Log in to your Splunk On-Call (VictorOps) portal",
            "2. Go to Integrations → Outgoing Webhooks",
            "3. Click 'Add Webhook'",
            "4. Set Event Type to: Any-Incident",
            "5. Paste the webhook URL above",
            "6. Save and test the webhook",
        ],
    })


@victorops_bp.route("/webhook/<user_id>", methods=["POST"])
def webhook(user_id: str):
    """Receive outbound webhook events from Splunk On-Call."""
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    creds = get_token_data(user_id, "victorops")
    if not creds:
        logger.warning(
            "[VICTOROPS] Webhook received for user %s with no VictorOps connection",
            sanitize(user_id),
        )
        return jsonify({"error": "Splunk On-Call not connected for this user"}), 404

    payload = request.get_json(silent=True) or {}

    # VictorOps sends a flat dot-notation payload. The phase lives at
    # STATE.CURRENT_ALERT_PHASE (UNACKED/ACKED/RESOLVED) or can be
    # inferred from INCIDENT.CURRENT_PHASE or ALERT.message_type.
    _VO_PHASE_MAP = {
        "UNACKED": "TRIGGERED",
        "ACKED": "ACKNOWLEDGED",
        "RESOLVED": "RESOLVED",
        "CRITICAL": "TRIGGERED",
        "WARNING": "TRIGGERED",
        "ACKNOWLEDGEMENT": "ACKNOWLEDGED",
        "RECOVERY": "RESOLVED",
        "INFO": "TRIGGERED",
    }
    raw_phase = (
        payload.get("STATE.CURRENT_ALERT_PHASE")
        or payload.get("INCIDENT.CURRENT_PHASE")
        or payload.get("ALERT.message_type")
        or payload.get("CURRENT_ALERT_PHASE")
        or ""
    ).upper()
    alert_phase = _VO_PHASE_MAP.get(raw_phase, raw_phase)

    entity_id = (
        payload.get("STATE.ENTITY_ID")
        or payload.get("ALERT.entity_id")
        or payload.get("ENTITY_ID")
        or ""
    )

    logger.info(
        "[VICTOROPS] Webhook received for user %s: raw_phase=%s -> phase=%s, entity=%s",
        sanitize(user_id),
        sanitize(raw_phase),
        sanitize(alert_phase),
        sanitize(entity_id),
    )

    if not alert_phase:
        logger.debug("[VICTOROPS] Ignoring webhook with no recognisable alert phase")
        return jsonify({"received": True, "reason": "no alert phase"})

    if alert_phase not in ("TRIGGERED", "ACKNOWLEDGED", "RESOLVED"):
        logger.debug("[VICTOROPS] Ignoring unrecognised alert phase: %s", alert_phase)
        return jsonify({"received": True, "reason": "unrecognised alert phase"})

    from routes.victorops.tasks import process_victorops_event

    metadata = {"headers": dict(request.headers), "remote_addr": request.remote_addr}
    process_victorops_event.delay(
        payload=payload,
        metadata=metadata,
        user_id=user_id,
    )

    logger.info(
        "[VICTOROPS] Enqueued event for processing: user=%s, phase=%s",
        sanitize(user_id),
        sanitize(alert_phase),
    )
    return jsonify({"received": True})
