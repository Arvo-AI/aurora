"""Shared ``POST /webhook-secret/rotate`` for connectors whose alert webhook is authenticated by a stored secret."""

import logging

from flask import jsonify

from utils.auth.rbac_decorators import require_permission
from utils.log_sanitizer import sanitize
from utils.web.webhook_auth import rotate_webhook_secret

logger = logging.getLogger(__name__)


def register_webhook_secret_routes(blueprint, provider: str, provider_name: str):
    """Register ``POST /webhook-secret/rotate`` on the given connector blueprint."""
    label = provider.upper()

    @blueprint.route("/webhook-secret/rotate", methods=["POST"])
    @require_permission("connectors", "write")
    def rotate_webhook_secret_route(user_id):
        """Issue a new webhook secret; the old one stops working immediately.

        The caller then copies the new webhook URL into the vendor.
        """
        try:
            secret = rotate_webhook_secret(user_id, provider)
        except Exception:
            logger.exception("[%s] Failed to rotate webhook secret for user %s", label, sanitize(user_id))
            return jsonify({"error": "Failed to rotate webhook secret"}), 500
        if not secret:
            return jsonify({"error": f"{provider_name} not connected"}), 404
        return jsonify({"success": True, "webhookSecret": secret})
