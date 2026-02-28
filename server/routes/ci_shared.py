"""Shared helpers for CI provider routes (Jenkins, CloudBees)."""

import logging

from flask import jsonify, request

from utils.auth.stateless_auth import get_user_id_from_request, get_user_preference, store_user_preference
from utils.web.cors_utils import create_cors_response

logger = logging.getLogger(__name__)


def register_rca_settings_routes(blueprint, provider: str, preference_key: str):
    """Register GET/PUT /rca-settings routes on the given blueprint."""
    label = provider.upper()

    @blueprint.route("/rca-settings", methods=["GET", "OPTIONS"])
    def get_rca_settings():
        if request.method == "OPTIONS":
            return create_cors_response()

        user_id = get_user_id_from_request()
        if not user_id:
            return jsonify({"error": "User authentication required"}), 401

        rca_enabled = get_user_preference(user_id, preference_key, default=True)
        return jsonify({"rcaEnabled": rca_enabled})

    @blueprint.route("/rca-settings", methods=["PUT", "OPTIONS"])
    def update_rca_settings():
        if request.method == "OPTIONS":
            return create_cors_response()

        user_id = get_user_id_from_request()
        if not user_id:
            return jsonify({"error": "User authentication required"}), 401

        try:
            data = request.get_json(force=True, silent=True) or {}
        except Exception:
            data = {}

        rca_enabled = data.get("rcaEnabled", True)
        if not isinstance(rca_enabled, bool):
            return jsonify({"error": "rcaEnabled must be a boolean"}), 400

        store_user_preference(user_id, preference_key, rca_enabled)
        logger.info("[%s] Updated RCA settings for user %s: rcaEnabled=%s", label, user_id, rca_enabled)

        return jsonify({"success": True, "rcaEnabled": rca_enabled})
