"""
Cloudflare API Routes - Authentication and Zones

Provides endpoints for:
1. Connecting a Cloudflare account (API token validation + storage)
2. Fetching DNS zones
3. Connection status and disconnect

Security:
- API token is stored in HashiCorp Vault (not in database)
- Only a secret reference is stored in the database
- Rate limiting applied to prevent brute force
- Input validation on all user-provided data
"""

import logging
import json
from flask import request, jsonify

from routes.cloudflare import cloudflare_bp
from utils.auth.rbac_decorators import require_permission
from utils.auth.token_management import store_tokens_in_db, get_token_data
from utils.secrets.secret_ref_utils import has_user_credentials, delete_user_secret
from utils.db.connection_utils import set_connection_status
from utils.web.limiter_ext import limiter
from utils.logging.secure_logging import mask_credential_value
from connectors.cloudflare_connector.auth import validate_api_token
from connectors.cloudflare_connector.api_client import CloudflareClient

logger = logging.getLogger(__name__)


@cloudflare_bp.route('/cloudflare/connect', methods=['POST', 'OPTIONS'])
@limiter.limit("10 per minute;50 per hour")
@require_permission("connectors", "write")
def cloudflare_connect(user_id):
    """
    Connect a Cloudflare account using an API token.

    Request body:
    {
        "apiToken": "Cloudflare API token (fine-grained)"
    }
    """
    try:
        data = request.get_json() or {}
        api_token = data.get('apiToken') or data.get('api_token')

        if not api_token:
            return jsonify({"error": "API token is required"}), 400

        api_token = api_token.strip()

        if len(api_token) < 20:
            return jsonify({"error": "Invalid API token format"}), 400

        logger.info(f"Cloudflare connect attempt for user {user_id}")

        success, token_info, error = validate_api_token(api_token)

        if not success:
            logger.warning(f"Cloudflare credential validation failed for user {user_id}: {error}")
            return jsonify({"error": error}), 401

        client = CloudflareClient(api_token)

        accounts_raw = client.list_accounts()
        accounts = [
            {"id": a.get("id"), "name": a.get("name"), "type": a.get("type")}
            for a in accounts_raw
        ]
        primary_account_id = accounts[0]["id"] if accounts else "unknown"
        primary_account_name = accounts[0]["name"] if accounts else None

        zones_count = len(client.list_zones())

        email = None
        try:
            user = client.get_current_user()
            email = user.get("email")
        except Exception:
            logger.warning("Could not fetch Cloudflare user email")

        token_data = {
            "api_token": api_token,
            "token_id": token_info.get("token_id"),
            "accounts": accounts,
            "email": email,
        }

        store_tokens_in_db(user_id, token_data, "cloudflare")
        set_connection_status(user_id, "cloudflare", primary_account_id, "connected")

        logger.info(f"Cloudflare connected for user {user_id}")

        return jsonify({
            "success": True,
            "message": "Cloudflare connected successfully",
            "accountName": primary_account_name,
            "accountCount": len(accounts),
            "zonesCount": zones_count,
            "email": email,
        })

    except Exception as e:
        logger.error(f"Error connecting Cloudflare for user: {e}", exc_info=True)
        return jsonify({"error": "Failed to connect Cloudflare"}), 500


@cloudflare_bp.route('/cloudflare/zones', methods=['GET', 'OPTIONS'])
@limiter.limit("30 per minute")
@require_permission("connectors", "read")
def cloudflare_zones_get(user_id):
    """Fetch Cloudflare DNS zones."""
    try:
        token_data = get_token_data(user_id, "cloudflare")
        if not token_data:
            return jsonify({
                "error": "Cloudflare not connected. Please connect your account.",
                "action": "CONNECT_REQUIRED",
            }), 401

        api_token = token_data.get("api_token")
        if not api_token:
            return jsonify({"error": "Invalid stored credentials"}), 401

        account_id = request.args.get("accountId")

        client = CloudflareClient(api_token)
        raw_zones = client.list_zones(account_id=account_id)

        zones = [
            {
                "id": z.get("id"),
                "name": z.get("name"),
                "status": z.get("status"),
                "account_id": z.get("account", {}).get("id"),
                "account_name": z.get("account", {}).get("name"),
                "plan": z.get("plan", {}).get("name"),
                "enabled": True,
            }
            for z in raw_zones
        ]

        # Merge saved preferences if any
        from utils.auth.stateless_auth import get_user_preference
        saved_prefs = get_user_preference(user_id, 'cloudflare_zones') or []
        saved_selections = {}
        if isinstance(saved_prefs, list):
            for z in saved_prefs:
                if isinstance(z, dict):
                    saved_selections[z.get('id')] = z.get('enabled', True)

        for zone in zones:
            zone_id = zone.get('id')
            if zone_id in saved_selections:
                zone['enabled'] = saved_selections[zone_id]

        return jsonify({"zones": zones})

    except Exception as e:
        logger.error(f"Error fetching Cloudflare zones: {e}", exc_info=True)
        return jsonify({"error": "Failed to fetch zones"}), 500


@cloudflare_bp.route('/cloudflare/zones', methods=['POST', 'OPTIONS'])
@limiter.limit("30 per minute")
@require_permission("connectors", "write")
def cloudflare_zones_post(user_id):
    """Save Cloudflare zone selections."""
    try:
        token_data = get_token_data(user_id, "cloudflare")
        if not token_data:
            return jsonify({
                "error": "Cloudflare not connected. Please connect your account.",
                "action": "CONNECT_REQUIRED",
            }), 401

        data = request.get_json(silent=True)
        if data is None:
            return jsonify({"error": "Invalid or missing JSON body"}), 400

        zones = data.get("zones", [])

        from utils.auth.stateless_auth import store_user_preference
        store_user_preference(user_id, 'cloudflare_zones', zones)

        logger.info(f"Saved Cloudflare zone selections for user {user_id}")
        return jsonify({"success": True, "message": "Zones saved"})

    except Exception as e:
        logger.error(f"Error saving Cloudflare zones: {e}", exc_info=True)
        return jsonify({"error": "Failed to save zones"}), 500


@cloudflare_bp.route('/cloudflare/status', methods=['GET', 'OPTIONS'])
@limiter.limit("60 per minute")
@require_permission("connectors", "read")
def cloudflare_status(user_id):
    """Check Cloudflare connection status by verifying the stored token."""
    try:
        has_creds = has_user_credentials(user_id, "cloudflare")
        if not has_creds:
            return jsonify({"connected": False, "provider": "cloudflare"})

        token_data = get_token_data(user_id, "cloudflare")
        if not token_data:
            return jsonify({"connected": False, "provider": "cloudflare"})

        api_token = token_data.get("api_token")
        if not api_token:
            return jsonify({"connected": False, "provider": "cloudflare"})

        success, _, error = validate_api_token(api_token)

        if not success:
            if error and any(kw in str(error) for kw in ("401", "403", "Unauthorized")):
                logger.warning(f"Cloudflare credentials invalid for user {user_id}: {error}")
                delete_user_secret(user_id, "cloudflare")
                return jsonify({"connected": False, "provider": "cloudflare"})
            logger.warning(f"Cloudflare API check failed (non-auth error): {error}")

        response = {
            "connected": True,
            "provider": "cloudflare",
        }

        email = token_data.get("email")
        if email:
            response["email"] = email

        accounts = token_data.get("accounts", [])
        if accounts:
            response["accountName"] = accounts[0].get("name")

        return jsonify(response)

    except Exception as e:
        logger.error(f"Error checking Cloudflare status: {e}", exc_info=True)
        has_creds = has_user_credentials(user_id, "cloudflare") if user_id else False
        return jsonify({"connected": has_creds, "provider": "cloudflare"}), 200


@cloudflare_bp.route('/cloudflare/disconnect', methods=['POST', 'OPTIONS'])
@limiter.limit("10 per minute")
@require_permission("connectors", "write")
def cloudflare_disconnect(user_id):
    """Disconnect the Cloudflare account."""
    try:
        token_data = get_token_data(user_id, "cloudflare")
        accounts = token_data.get("accounts", []) if token_data else []
        account_id = accounts[0]["id"] if accounts else "unknown"

        delete_user_secret(user_id, "cloudflare")
        set_connection_status(user_id, "cloudflare", account_id, "disconnected")

        # Clear zone preferences
        from utils.auth.stateless_auth import store_user_preference
        try:
            store_user_preference(user_id, 'cloudflare_zones', None)
        except Exception:
            pass

        logger.info(f"Cloudflare disconnected for user {user_id}")

        return jsonify({
            "success": True,
            "message": "Cloudflare disconnected successfully",
        })

    except Exception as e:
        logger.error(f"Error disconnecting Cloudflare: {e}", exc_info=True)
        return jsonify({"error": "Failed to disconnect Cloudflare"}), 500
