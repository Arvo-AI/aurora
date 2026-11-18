"""Authenticated one-time callback from Aurora's central browser runner."""

from flask import Blueprint, jsonify, request

from tasks.browser_test_runs import complete

github_browser_test_callback_bp = Blueprint("github_browser_test_callback", __name__)


@github_browser_test_callback_bp.route("/browser-test/callback", methods=["POST"])
def browser_test_callback():
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    body = request.get_json(silent=True) or {}
    try:
        status = complete(
            str(body.get("run_id") or ""), token, body.get("result") or {},
            int(body.get("workflow_run_id") or 0), str(body.get("workflow_url") or ""),
        )
    except PermissionError:
        return jsonify({"error": "invalid callback credentials"}), 401
    except LookupError:
        return jsonify({"error": "unknown run"}), 404
    except (TypeError, ValueError):
        return jsonify({"error": "invalid callback payload"}), 400
    return jsonify({"status": status})
