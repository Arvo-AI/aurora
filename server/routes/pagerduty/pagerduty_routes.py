"""PagerDuty integration routes.

Supports PagerDuty V3 webhooks only. V1/V2 webhooks are not supported.
For webhook configuration, use the PagerDuty Webhook Subscriptions API.
"""

import json
import logging
import os
import urllib.parse
from flask import Blueprint, jsonify, request, redirect

from utils.db.connection_pool import db_pool
from utils.web.cors_utils import create_cors_response
from utils.flags.feature_flags import is_pagerduty_oauth_enabled
from utils.auth.stateless_auth import get_user_id_from_request
from utils.auth.token_management import get_token_data, store_tokens_in_db
from routes.pagerduty.oauth_utils import get_auth_url, exchange_code_for_token, refresh_token_if_needed
from routes.pagerduty.pagerduty_helpers import PagerDutyClient, PagerDutyAPIError, validate_token, error_response

logger = logging.getLogger(__name__)
pagerduty_bp = Blueprint("pagerduty", __name__)

FRONTEND_URL = os.getenv("FRONTEND_URL")


def _validate_v3_webhook(payload: dict) -> tuple[bool, str]:
    """Validate PagerDuty V3 webhook structure.
    
    Args:
        payload: The webhook payload to validate
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    if not isinstance(payload, dict):
        return False, "Payload must be a JSON object"
    
    if "event" not in payload:
        return False, "Missing 'event' field in V3 webhook"
    
    event = payload["event"]
    if not isinstance(event, dict):
        return False, "'event' must be an object"
    
    if "event_type" not in event:
        return False, "Missing 'event_type' in event"
    
    if "resource_type" not in event:
        return False, "Missing 'resource_type' in event"
    
    return True, ""


@pagerduty_bp.route("", methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"])
def pagerduty_api():
    """Unified PagerDuty endpoint."""
    if request.method == "OPTIONS":
        return create_cors_response()
    
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "Authentication required"}), 401
    
    if request.method == "GET":
        creds = get_token_data(user_id, "pagerduty")
        if not creds:
            return jsonify({"connected": False})
        
        if creds.get("auth_type") == "oauth":
            success, refreshed = refresh_token_if_needed(creds)
            if success and refreshed:
                try:
                    store_tokens_in_db(user_id, {**creds, **refreshed}, "pagerduty")
                    creds.update(refreshed)
                except Exception:
                    pass
        
        return jsonify({"connected": True, "displayName": creds.get("display_name", "PagerDuty"), "validatedAt": creds.get("validated_at"), "authType": creds.get("auth_type", "api_token"), "capabilities": creds.get("capabilities", {}), "externalUserEmail": creds.get("external_user_email"), "externalUserName": creds.get("external_user_name"), "accountSubdomain": creds.get("account_subdomain")})
    
    elif request.method in ["POST", "PATCH"]:
        data = request.get_json(force=True, silent=True) or {}
        token = data.get("token")
        
        if not token or not isinstance(token, str):
            return jsonify({"error": "Token required"}), 400
        
        token = token.strip()
        
        if request.method == "PATCH":
            existing = get_token_data(user_id, "pagerduty")
            if not existing:
                return jsonify({"error": "Not connected"}), 404
            if existing.get("auth_type") == "oauth":
                return jsonify({"error": "Cannot rotate OAuth tokens"}), 400
            display_name = existing.get("display_name", "PagerDuty")
        else:
            display_name = data.get("displayName", "PagerDuty")
        
        logger.info(f"[PAGERDUTY] Validating API token for user {user_id}")
        try:
            token_info = validate_token(PagerDutyClient(api_token=token))
            logger.info(f"[PAGERDUTY] Token validated successfully for user {user_id}")
        except PagerDutyAPIError as e:
            logger.warning(f"[PAGERDUTY] Token validation failed for user {user_id}: {str(e)}")
            return error_response(e)
        
        # Store token data
        token_data = {
            "auth_type": "api_token",
            "api_token": token,
            "display_name": display_name,
            **token_info
        }
        
        try:
            store_tokens_in_db(user_id, token_data, "pagerduty")
        except Exception as e:
            return jsonify({"error": "Storage failed"}), 500
        
        return jsonify({"success": True, "connected": True, "displayName": display_name, **token_info})
    
    elif request.method == "DELETE":
        try:
            with db_pool.get_admin_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM user_tokens WHERE user_id = %s AND provider = %s", (user_id, "pagerduty"))
                conn.commit()
            return jsonify({"success": True})
        except Exception:
            return jsonify({"error": "Disconnect failed"}), 500


@pagerduty_bp.route("/oauth/login", methods=["POST", "OPTIONS"])
def oauth_login():
    """Initiate OAuth flow."""
    if request.method == "OPTIONS":
        return create_cors_response()
    
    if not is_pagerduty_oauth_enabled():
        return jsonify({"error": "PagerDuty OAuth is not enabled"}), 403
    
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "Authentication required"}), 401
    
    try:
        oauth_url = get_auth_url(state=urllib.parse.quote(user_id))
        return jsonify({"oauth_url": oauth_url})
    except Exception as e:
        return jsonify({"error": "OAuth init failed"}), 500


@pagerduty_bp.route("/oauth/callback", methods=["GET"])
def oauth_callback():
    """Handle OAuth callback."""
    if not is_pagerduty_oauth_enabled():
        callback_url = f"{FRONTEND_URL}/pagerduty/auth/callback"
        return redirect(f"{callback_url}?oauth=failed&error=oauth_not_enabled")
    error = request.args.get("error")
    code = request.args.get("code")
    state = request.args.get("state")
    
    callback_url = f"{FRONTEND_URL}/pagerduty/auth/callback"
    
    if error or not code or not state:
        return redirect(f"{callback_url}?oauth=failed&error={error or 'invalid'}")
    
    try:
        user_id = urllib.parse.unquote(state)
        token_data = exchange_code_for_token(code)
        
        if not token_data or not (access_token := token_data.get("access_token")):
            return redirect(f"{callback_url}?oauth=failed&error=exchange_failed")
        
        from time import time
        expires_at = int(time()) + token_data.get("expires_in", 3600)
        
        try:
            token_info = validate_token(PagerDutyClient(oauth_token=access_token))
        except PagerDutyAPIError:
            return redirect(f"{callback_url}?oauth=failed&error=validation_failed")
        
        # Build OAuth token data
        oauth_token_data = {
            "auth_type": "oauth",
            "access_token": access_token,
            "refresh_token": token_data.get("refresh_token"),
            "expires_at": expires_at,
            "display_name": "PagerDuty",
            **token_info
        }
        
        store_tokens_in_db(user_id, oauth_token_data, "pagerduty")
        return redirect(f"{callback_url}?oauth=success")
    except Exception:
        return redirect(f"{callback_url}?oauth=failed&error=unexpected")


@pagerduty_bp.route("/webhook-url", methods=["GET", "OPTIONS"])
def get_webhook_url():
    """Get the webhook URL that should be configured in PagerDuty."""
    if request.method == "OPTIONS":
        return create_cors_response()
    
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401
    
    # Use ngrok URL for development if available, otherwise use backend URL
    ngrok_url = os.getenv("NGROK_URL", "").rstrip("/")
    backend_url = os.getenv("NEXT_PUBLIC_BACKEND_URL", "").rstrip("/")

    # For development, prefer ngrok URL if available
    if ngrok_url and backend_url.startswith("http://localhost"):
        base_url = ngrok_url
    else:
        base_url = backend_url

    webhook_url = f"{base_url}/pagerduty/webhook/{user_id}"
    
    return jsonify({
        "webhookUrl": webhook_url,
        "instructions": [
            "1. Go to your PagerDuty account",
            "2. Navigate to Integrations → Generic Webhooks (v3)",
            "3. Click 'New Webhook'",
            "4. Paste the webhook URL above",
            "5. Select the events you want to subscribe to (incident.triggered, incident.acknowledged, incident.resolved)",
            "6. Save the webhook configuration"
        ]
    })


def _validate_v3_webhook(payload: dict) -> tuple[bool, str]:
    """Validate PagerDuty V3 webhook structure.
    
    Args:
        payload: The webhook payload to validate
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    if not isinstance(payload, dict):
        return False, "Payload must be a JSON object"
    
    if "event" not in payload:
        return False, "Missing 'event' field in V3 webhook"
    
    event = payload["event"]
    if not isinstance(event, dict):
        return False, "'event' must be an object"
    
    if "event_type" not in event:
        return False, "Missing 'event_type' in event"
    
    if "resource_type" not in event:
        return False, "Missing 'resource_type' in event"
    
    return True, ""


@pagerduty_bp.route("/webhook/<user_id>", methods=["POST", "OPTIONS"])
def webhook(user_id: str):
    """Receive V3 webhook events from PagerDuty."""
    if request.method == "OPTIONS":
        return create_cors_response()
    
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400
    
    # Check if user has PagerDuty connected
    creds = get_token_data(user_id, "pagerduty")
    if not creds:
        logger.warning("[PAGERDUTY] Webhook received for user %s with no PagerDuty connection", user_id)
        return jsonify({"error": "PagerDuty not connected for this user"}), 404
    
    payload = request.get_json(silent=True) or {}
    
    # Log full payload (temporary)
    logger.info("[PAGERDUTY] Full webhook payload for user %s:\n%s", user_id, json.dumps(payload, indent=2))
    
    # Validate V3 webhook structure
    is_valid, error_msg = _validate_v3_webhook(payload)
    if not is_valid:
        logger.warning("[PAGERDUTY] Invalid V3 webhook for user %s: %s", user_id, error_msg)
        return jsonify({"error": error_msg}), 400
    
    event = payload["event"]
    event_type = event["event_type"]
    resource_type = event["resource_type"]
    
    # Log V3 webhook receipt
    logger.info(
        "[PAGERDUTY] V3 webhook received for user %s: type=%s, resource=%s, id=%s",
        user_id,
        event_type,
        resource_type,
        event.get("id")
    )
    
    # Only process incident events
    if resource_type != "incident":
        logger.debug("[PAGERDUTY] Ignoring non-incident event: %s", resource_type)
        return jsonify({"received": True, "reason": "non-incident event"})
    
    # Filter for specific incident event types (including custom field updates)
    if event_type not in ["incident.triggered", "incident.acknowledged", "incident.resolved", "incident.custom_field_values.updated"]:
        logger.debug("[PAGERDUTY] Ignoring incident event type: %s", event_type)
        return jsonify({"received": True, "reason": "event type not monitored"})
    
    # Enqueue for background processing
    from routes.pagerduty.tasks import process_pagerduty_event
    
    metadata = {"headers": dict(request.headers), "remote_addr": request.remote_addr}
    process_pagerduty_event.delay(
        raw_payload=payload,
        event_data=event,
        metadata=metadata,
        user_id=user_id
    )
    
    logger.info("[PAGERDUTY] Enqueued event for processing: user=%s, type=%s", user_id, event_type)
    return jsonify({"received": True})


