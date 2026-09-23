"""PagerDuty integration routes.

Supports PagerDuty V3 webhooks only. V1/V2 webhooks are not supported.
For webhook configuration, use the PagerDuty Webhook Subscriptions API.
"""

import json
import logging
import os
import urllib.parse
from flask import Blueprint, jsonify, request, redirect

from utils.flags.feature_flags import is_pagerduty_oauth_enabled
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.auth.rbac_decorators import require_permission
from utils.auth.enforcer import enforce_with_reload
from utils.auth.stateless_auth import (
    get_org_id_from_request,
    get_org_id_for_user,
    get_org_preference,
    store_org_preference,
)
from utils.log_sanitizer import sanitize
from routes.pagerduty.oauth_utils import get_auth_url, exchange_code_for_token, refresh_and_store_if_needed
from routes.pagerduty.pagerduty_helpers import (
    PD_READ_ONLY_ROLES,
    PagerDutyClient,
    PagerDutyAPIError,
    validate_token,
    error_response,
)
from utils.secrets.secret_ref_utils import delete_user_secret

logger = logging.getLogger(__name__)
pagerduty_bp = Blueprint("pagerduty", __name__)

FRONTEND_URL = os.getenv("FRONTEND_URL")
NOTES_PREFERENCE_KEY = "pagerduty_incident_notes"


def _capabilities(creds: dict) -> dict:
    """Stored capabilities, defaulted for credentials saved before write detection existed."""
    return {"can_read_incidents": True, "can_write_incidents": False, **(creds.get("capabilities") or {})}


def _unwritable_reason(creds: dict, capabilities: dict) -> tuple[str, str]:
    """(message, code) explaining why these credentials cannot post notes."""
    access = capabilities.get("api_key_access")
    role = creds.get("external_user_role")
    if access == "account":
        return (
            "This is an account-level API key. PagerDuty notes need a user API token "
            "from a user with write access.",
            "account_level_key",
        )
    if role in PD_READ_ONLY_ROLES:
        # Checked before the OAuth scope: no re-consent by this user can add write access
        remedy = (
            "Reconnect as a user with write access." if access == "oauth"
            else "Use a token from a user with write access."
        )
        return (
            f"The PagerDuty user behind this connection has the {role} role, which cannot add notes. {remedy}",
            "read_only_role",
        )
    if access == "oauth" and "incidents.write" not in (creds.get("granted_scopes") or "").split():
        return (
            "This PagerDuty connection was authorized without incident write access. "
            "Disconnect and reconnect PagerDuty to grant it.",
            "oauth_scope_missing",
        )
    return (
        "This PagerDuty token cannot post notes. Use a user API token from a user with write access.",
        "read_only_key",
    )


def _status_payload(creds: dict) -> dict:
    """The connection status the client stores as PagerDutyStatus (status, connect and rotate)."""
    capabilities = _capabilities(creds)
    payload = {
        "connected": True,
        "displayName": creds.get("display_name", "PagerDuty"),
        "validatedAt": creds.get("validated_at"),
        "authType": creds.get("auth_type", "api_token"),
        "capabilities": capabilities,
        "externalUserEmail": creds.get("external_user_email"),
        "externalUserName": creds.get("external_user_name"),
        "externalUserRole": creds.get("external_user_role"),
        "accountSubdomain": creds.get("account_subdomain"),
    }
    if capabilities.get("can_write_incidents") is not True:
        # Single source for the client's "why can't I turn notes on" text
        payload["notesUnwritableReason"] = _unwritable_reason(creds, capabilities)[0]
    return payload


def _disable_notes_if_unwritable(org_id, capabilities: dict) -> bool:
    """Turn the org's notes toggle off when the active credentials cannot post notes."""
    if not org_id or capabilities.get("can_write_incidents") is True:
        return False
    if not get_org_preference(org_id, NOTES_PREFERENCE_KEY, default=False):
        return False
    store_org_preference(org_id, NOTES_PREFERENCE_KEY, False)
    logger.info("[PAGERDUTY] Disabled incident notes for org: credentials cannot write incidents")
    return True


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


@pagerduty_bp.route("", methods=["GET"])
@require_permission("connectors", "read")
def pagerduty_status(user_id):
    """Get PagerDuty connection status."""
    creds = get_token_data(user_id, "pagerduty")
    if not creds:
        return jsonify({"connected": False})

    if creds.get("auth_type") == "oauth" and is_pagerduty_oauth_enabled():
        success, creds = refresh_and_store_if_needed(user_id, creds)
        if not success:
            return jsonify({"connected": False, "error": "OAuth token expired, please reconnect"})

    return jsonify(_status_payload(creds))


@pagerduty_bp.route("", methods=["POST", "PATCH"])
@require_permission("connectors", "write")
def pagerduty_connect(user_id):
    """Connect or update PagerDuty API token."""
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
        logger.warning("[PAGERDUTY] Token validation failed for user %s: %s", user_id, e)
        return error_response(e)

    token_data = {
        "auth_type": "api_token",
        "api_token": token,
        "display_name": display_name,
        **token_info
    }

    try:
        store_tokens_in_db(user_id, token_data, "pagerduty")
    except Exception:
        logger.exception("[PAGERDUTY] Failed to store token data for user %s", user_id)
        return jsonify({"error": "Storage failed"}), 500

    # A key rotated to one that cannot post notes must not leave the toggle on
    notes_disabled = False
    try:
        org_id = get_org_id_from_request() or get_org_id_for_user(user_id)
        notes_disabled = _disable_notes_if_unwritable(org_id, token_info["capabilities"])
    except Exception:
        logger.exception("[PAGERDUTY] Failed to reconcile incident-notes preference")

    return jsonify({"success": True, "notesDisabled": notes_disabled, **_status_payload(token_data)})


@pagerduty_bp.route("", methods=["DELETE"])
@require_permission("connectors", "write")
def pagerduty_disconnect(user_id):
    """Disconnect PagerDuty."""
    try:
        success, deleted = delete_user_secret(user_id, "pagerduty")
        if not success:
            logger.warning("[PAGERDUTY] Failed to clean up secrets during disconnect")
            return jsonify({"success": False, "error": "Failed to delete stored credentials"}), 500

        logger.info("[PAGERDUTY] Disconnected provider (deleted %d token rows)", deleted)
        return jsonify({"success": True, "deleted": deleted})
    except Exception:
        logger.exception("[PAGERDUTY] Disconnect failed")
        return jsonify({"error": "Disconnect failed"}), 500


@pagerduty_bp.route("/oauth/login", methods=["POST"])
@require_permission("connectors", "write")
def oauth_login(user_id):
    """Initiate OAuth flow."""
    if not is_pagerduty_oauth_enabled():
        return jsonify({"error": "PagerDuty OAuth is not enabled"}), 403
    
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
        # Use a fixed error code rather than echoing user-provided error string
        error_code = "access_denied" if error == "access_denied" else "invalid"
        return redirect(f"{callback_url}?oauth=failed&error={error_code}")
    
    try:
        user_id = urllib.parse.unquote(state)
        token_data = exchange_code_for_token(code)
        
        if not token_data or not (access_token := token_data.get("access_token")):
            return redirect(f"{callback_url}?oauth=failed&error=exchange_failed")
        
        from time import time
        expires_at = int(time()) + token_data.get("expires_in", 3600)
        
        # PagerDuty silently drops unregistered scopes, so keep what was actually granted
        granted_scopes = token_data.get("scope", "")
        try:
            token_info = validate_token(PagerDutyClient(oauth_token=access_token), granted_scopes=granted_scopes)
        except PagerDutyAPIError:
            return redirect(f"{callback_url}?oauth=failed&error=validation_failed")
        
        # Build OAuth token data
        oauth_token_data = {
            "auth_type": "oauth",
            "access_token": access_token,
            "refresh_token": token_data.get("refresh_token"),
            "expires_at": expires_at,
            "display_name": "PagerDuty",
            "granted_scopes": granted_scopes,
            **token_info
        }
        
        store_tokens_in_db(user_id, oauth_token_data, "pagerduty")
        # A reconnect without incidents.write must not leave the toggle on
        try:
            _disable_notes_if_unwritable(get_org_id_for_user(user_id), token_info["capabilities"])
        except Exception:
            logger.exception("[PAGERDUTY] Failed to reconcile incident-notes preference after OAuth")
        return redirect(f"{callback_url}?oauth=success")
    except Exception:
        return redirect(f"{callback_url}?oauth=failed&error=unexpected")


def _oauth_client_for(user_id: str, creds: dict):
    """(client, creds, error_response) for an OAuth connection; refreshes and persists the token first."""
    if not is_pagerduty_oauth_enabled():
        return None, creds, (jsonify({"error": "PagerDuty OAuth is not enabled", "code": "oauth_disabled"}), 403)
    success, creds = refresh_and_store_if_needed(user_id, creds)
    if not success:
        return None, creds, (jsonify({"error": "OAuth token expired, please reconnect", "code": "token_expired"}), 401)
    return PagerDutyClient(oauth_token=creds.get("access_token")), creds, None


@pagerduty_bp.route("/notes/enable", methods=["POST"])
@require_permission("connectors", "write")
def enable_incident_notes(user_id):
    """Turn on RCA notes for the org after re-proving the credentials can write incidents.

    This is the only way to set the pagerduty_incident_notes preference to
    true; the generic preference endpoints reject it. Disabling goes through
    the generic endpoints.
    """
    org_id = get_org_id_from_request() or get_org_id_for_user(user_id)
    if not org_id:
        return jsonify({"error": "Could not resolve organization"}), 400
    if not enforce_with_reload(user_id, org_id, "notification_settings", "write"):
        return jsonify({"error": "Forbidden - editor or admin role required"}), 403

    creds = get_token_data(user_id, "pagerduty")
    if not creds:
        return jsonify({"error": "PagerDuty is not connected. Connect PagerDuty first.", "code": "not_connected"}), 404

    if creds.get("auth_type") == "oauth":
        client, creds, error = _oauth_client_for(user_id, creds)
        if error:
            return error
    else:
        client = PagerDutyClient(api_token=creds.get("api_token"))

    try:
        token_info = validate_token(client, granted_scopes=creds.get("granted_scopes", ""))
    except PagerDutyAPIError as e:
        logger.warning("[PAGERDUTY] Re-validation for notes failed for user %s: %s", user_id, e)
        return error_response(e)

    creds = {**creds, **token_info}
    try:
        store_tokens_in_db(user_id, creds, "pagerduty")
    except Exception:
        logger.exception("[PAGERDUTY] Failed to store re-validated token data for user %s", user_id)
        return jsonify({"error": "Storage failed"}), 500

    capabilities = token_info["capabilities"]
    if capabilities.get("can_write_incidents") is not True:
        message, code = _unwritable_reason(creds, capabilities)
        # The toggle may still be on from a previous, writable credential
        _disable_notes_if_unwritable(org_id, capabilities)
        return jsonify({"error": message, "code": code}), 403

    store_org_preference(org_id, NOTES_PREFERENCE_KEY, True)
    # store_org_preference swallows DB errors; confirm the write landed
    if get_org_preference(org_id, NOTES_PREFERENCE_KEY, default=False) is not True:
        logger.error("[PAGERDUTY] Incident-notes preference did not persist for org")
        return jsonify({"error": "Failed to save preference"}), 500

    logger.info("[PAGERDUTY] Incident notes enabled for org by user %s", user_id)
    return jsonify({"enabled": True})


@pagerduty_bp.route("/webhook-url", methods=["GET"])
@require_permission("connectors", "read")
def get_webhook_url(user_id):
    """Get the webhook URL that should be configured in PagerDuty."""
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


@pagerduty_bp.route("/webhook/<user_id>", methods=["POST"])
def webhook(user_id: str):
    """Receive V3 webhook events from PagerDuty."""
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400
    
    # Check if user has PagerDuty connected
    creds = get_token_data(user_id, "pagerduty")
    if not creds:
        logger.warning("[PAGERDUTY] Webhook received for user %s with no PagerDuty connection", sanitize(user_id))
        return jsonify({"error": "PagerDuty not connected for this user"}), 404
    
    payload = request.get_json(silent=True) or {}
    
    # Log raw payload
    logger.info("[PAGERDUTY] Raw webhook payload for user %s: %s", sanitize(user_id), sanitize(json.dumps(payload)))
    
    # Validate V3 webhook structure
    is_valid, error_msg = _validate_v3_webhook(payload)
    if not is_valid:
        logger.warning("[PAGERDUTY] Invalid V3 webhook for user %s: %s", sanitize(user_id), error_msg)
        return jsonify({"error": error_msg}), 400
    
    event = payload["event"]
    event_type = event["event_type"]
    resource_type = event["resource_type"]
    
    # Log V3 webhook receipt
    logger.info(
        "[PAGERDUTY] V3 webhook received for user %s: type=%s, resource=%s, id=%s",
        sanitize(user_id),
        sanitize(event_type),
        sanitize(resource_type),
        sanitize(event.get("id"))
    )
    
    # Only process incident events
    if resource_type != "incident":
        logger.debug("[PAGERDUTY] Ignoring non-incident event: %s", resource_type)
        return jsonify({"received": True, "reason": "non-incident event"})
    
    # Filter for specific incident event types (including custom field updates).
    # Never add incident.annotated here: Aurora posts RCA notes back to PagerDuty
    # incidents, and reacting to note events would loop.
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
    
    logger.info("[PAGERDUTY] Enqueued event for processing: user=%s, type=%s", sanitize(user_id), sanitize(event_type))
    return jsonify({"received": True})


