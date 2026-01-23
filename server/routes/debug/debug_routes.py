import logging
from datetime import datetime
from flask import Blueprint, request, jsonify, session, Response
import flask
from utils.web.cors_utils import create_cors_response
from utils.auth.stateless_auth import get_user_id_from_request
from utils.auth.token_management import get_token_data

debug_util_bp = Blueprint("debug_util_bp", __name__)


@debug_util_bp.route("/debug/user-info", methods=["GET", "OPTIONS"])
def debug_user_info():
    if flask.request.method == 'OPTIONS':
        return create_cors_response()
    try:
        user_id = get_user_id_from_request()
        debug_info = {
            "time": datetime.utcnow().isoformat(),
            "user_id": user_id,
            "session_keys": list(session.keys()),
            "request_headers": dict(request.headers),
            "request_args": dict(request.args),
        }
        if user_id:
            token = get_token_data(user_id, "gcp") or {}
            debug_info["token_keys"] = list(token.keys())
        return jsonify(debug_info)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@debug_util_bp.route("/test-endpoint", methods=["GET", "OPTIONS"])
def test_endpoint():
    if flask.request.method == 'OPTIONS':
        return create_cors_response()
    return jsonify({
        "message": "Test endpoint working",
        "method": request.method,
        "headers": dict(request.headers),
        "args": dict(request.args),
    })
