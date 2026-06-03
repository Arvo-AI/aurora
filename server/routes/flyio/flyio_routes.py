"""
Fly.io API Routes - Authentication, Status, and Disconnect

Provides endpoints for:
1. Connecting a Fly.io organization (API token validation + tier detection + storage)
2. Connection status
3. Disconnect

Security:
- API token is stored in HashiCorp Vault (not in database)
- Only a secret reference is stored in the database
- Permission tier auto-detected (readonly vs full)
"""

import logging
import re
from flask import request, jsonify

from routes.flyio import flyio_bp
from utils.auth.rbac_decorators import require_permission
from utils.auth.token_management import store_tokens_in_db, get_token_data
from utils.secrets.secret_ref_utils import delete_user_secret
from utils.db.connection_utils import set_connection_status
from utils.web.limiter_ext import limiter
from connectors.flyio_connector.auth import validate_flyio_token
from connectors.flyio_connector.api_client import FlyioClient

logger = logging.getLogger(__name__)


@flyio_bp.route('/flyio/connect', methods=['POST'])
@limiter.limit("10 per minute;50 per hour")
@require_permission("connectors", "write")
def flyio_connect(user_id):
    """
    Connect a Fly.io organization using an API token.

    Request body:
    {
        "apiToken": "Fly.io org-scoped API token",
        "orgSlug": "organization slug (e.g. 'personal' or 'my-company')"
    }
    """
    try:
        data = request.get_json() or {}

        api_token = data.get('apiToken')
        org_slug = data.get('orgSlug')

        if not api_token or not org_slug:
            return jsonify({"error": "API token and organization slug are required"}), 400

        api_token = api_token.strip()
        org_slug = org_slug.strip().lower()

        match = re.search(r'fly\.io/dashboard/([^/?#]+)', org_slug)
        if match:
            org_slug = match.group(1)

        logger.info(f"Fly.io connect attempt for user {user_id}, org: {org_slug}")

        success, token_info, error = validate_flyio_token(api_token, org_slug)
        if not success:
            logger.warning(f"Fly.io credential validation failed for user {user_id}: {error}")
            return jsonify({"error": error}), 401

        token_data = {
            "api_token": api_token,
            "org_slug": org_slug,
            "tier": token_info["tier"],
            "apps": token_info["apps"],
        }

        store_tokens_in_db(user_id, token_data, "flyio")
        set_connection_status(user_id, "flyio", org_slug, "connected")

        logger.info(f"Fly.io connected for user {user_id}, org: {org_slug}, tier: {token_info['tier']}, apps: {len(token_info['apps'])}")

        return jsonify({
            "org_slug": org_slug,
            "tier": token_info["tier"],
            "apps": token_info["apps"],
        }), 200

    except Exception as e:
        logger.error(f"Fly.io connect error for user {user_id}: {e}", exc_info=True)
        return jsonify({"error": "An unexpected error occurred. Please try again."}), 500


@flyio_bp.route('/flyio/status', methods=['GET'])
@require_permission("connectors", "read")
def flyio_status(user_id):
    """
    Check Fly.io connection status.

    Pass ?validate=true to do a live check against the Fly.io API
    (used on the manage page). Without it, reads from stored data (fast).
    """
    try:
        token_data = get_token_data(user_id, "flyio")
        if not token_data:
            return jsonify({"connected": False}), 200

        org_slug = token_data.get("org_slug")
        tier = token_data.get("tier", "readonly")
        apps = token_data.get("apps", [])

        if request.args.get("validate", "").lower() == "true":
            api_token = token_data.get("api_token")
            if not api_token or not org_slug:
                return jsonify({"connected": False}), 200

            live_apps = FlyioClient(api_token, org_slug).list_apps()
            if live_apps is None:
                delete_user_secret(user_id, "flyio")
                set_connection_status(user_id, "flyio", org_slug, "disconnected")
                return jsonify({"connected": False, "reason": "token_invalid"}), 200

            apps = [
                {"name": a.get("name", a.get("id", "unknown")), "status": a.get("status", "unknown")}
                for a in live_apps
            ]

        return jsonify({
            "connected": True,
            "org_slug": org_slug,
            "tier": tier,
            "apps": apps,
        }), 200

    except Exception as e:
        logger.error(f"Fly.io status check error for user {user_id}: {e}")
        return jsonify({"connected": False}), 200


@flyio_bp.route('/flyio/disconnect', methods=['DELETE'])
@require_permission("connectors", "write")
def flyio_disconnect(user_id):
    """Disconnect Fly.io integration."""
    try:
        token_data = get_token_data(user_id, "flyio")
        org_slug = token_data.get("org_slug", "unknown") if token_data else "unknown"

        delete_user_secret(user_id, "flyio")
        set_connection_status(user_id, "flyio", org_slug, "disconnected")

        logger.info(f"Fly.io disconnected for user {user_id}")
        return jsonify({"success": True, "message": "Fly.io disconnected successfully"}), 200

    except Exception as e:
        logger.error(f"Fly.io disconnect error for user {user_id}: {e}", exc_info=True)
        return jsonify({"error": "Failed to disconnect. Please try again."}), 500
