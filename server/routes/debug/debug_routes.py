import logging
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify, session, Response
from utils.auth.rbac_decorators import require_auth_only
from utils.auth.token_management import get_token_data

debug_util_bp = Blueprint("debug_util_bp", __name__)


@debug_util_bp.route("/debug/user-info", methods=["GET"])
@require_auth_only
def debug_user_info(user_id):
    try:
        debug_info = {
            "time": datetime.now(timezone.utc).isoformat(),
            "user_id": user_id,
            "session_keys": list(session.keys()),
            "request_headers": dict(request.headers),
            "request_args": dict(request.args),
        }
        token = get_token_data(user_id, "gcp") or {}
        debug_info["token_keys"] = list(token.keys())
        return jsonify(debug_info)
    except Exception as e:
        logging.error(f"Error in debug user-info endpoint: {e}", exc_info=True)
        return jsonify({"error": "Failed to retrieve debug info"}), 500


@debug_util_bp.route("/test-endpoint", methods=["GET"])
def test_endpoint():
    return jsonify({
        "message": "Test endpoint working",
        "method": request.method,
        "headers": dict(request.headers),
        "args": dict(request.args),
    })
