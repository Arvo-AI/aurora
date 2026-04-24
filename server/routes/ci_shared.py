"""Shared helpers for CI provider routes (Jenkins, CloudBees)."""

import logging

from flask import jsonify, request

from utils.auth.stateless_auth import get_user_preference, store_user_preference
from utils.log_sanitizer import sanitize
from utils.web.cors_utils import create_cors_response
from utils.auth.rbac_decorators import require_permission

logger = logging.getLogger(__name__)


def register_rca_settings_routes(blueprint, provider: str, preference_key: str):
    """Register GET/PUT /rca-settings routes on the given blueprint."""
    label = provider.upper()

    @blueprint.route("/rca-settings", methods=["GET", "OPTIONS"])
    @require_permission("connectors", "read")
    def get_rca_settings(user_id):
        rca_enabled = get_user_preference(user_id, preference_key, default=True)
        return jsonify({"rcaEnabled": rca_enabled})

    @blueprint.route("/rca-settings", methods=["PUT", "OPTIONS"])
    @require_permission("connectors", "write")
    def update_rca_settings(user_id):
        try:
            data = request.get_json(force=True, silent=True) or {}
        except Exception:
            data = {}

        rca_enabled = data.get("rcaEnabled", True)
        if not isinstance(rca_enabled, bool):
            return jsonify({"error": "rcaEnabled must be a boolean"}), 400

        store_user_preference(user_id, preference_key, rca_enabled)
        logger.info("[%s] Updated RCA settings for user %s: rcaEnabled=%s", sanitize(label), sanitize(user_id), rca_enabled)

        return jsonify({"success": True, "rcaEnabled": rca_enabled})
