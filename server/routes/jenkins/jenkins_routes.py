import logging
from typing import Any, Dict, Optional

from flask import Blueprint, jsonify, request

from connectors.jenkins_connector.api_client import JenkinsClient
from utils.db.connection_pool import db_pool
from utils.web.cors_utils import create_cors_response
from utils.auth.stateless_auth import get_user_id_from_request
from utils.auth.token_management import get_token_data, store_tokens_in_db

logger = logging.getLogger(__name__)

jenkins_bp = Blueprint("jenkins", __name__)

JENKINS_PROVIDER = "jenkins"


def _get_stored_jenkins_credentials(user_id: str) -> Optional[Dict[str, Any]]:
    try:
        return get_token_data(user_id, JENKINS_PROVIDER)
    except Exception as exc:
        logger.error("Failed to retrieve Jenkins credentials for user %s: %s", user_id, exc)
        return None


def _build_client(creds: Dict[str, Any]) -> Optional[JenkinsClient]:
    base_url = creds.get("base_url")
    username = creds.get("username")
    api_token = creds.get("api_token")
    if not base_url or not username or not api_token:
        return None
    return JenkinsClient(base_url=base_url, username=username, api_token=api_token)


@jenkins_bp.route("/connect", methods=["POST", "OPTIONS"])
def connect():
    """Validate and store Jenkins credentials."""
    if request.method == "OPTIONS":
        return create_cors_response()

    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    user_id = data.get("userId") or get_user_id_from_request()
    base_url = data.get("baseUrl", "").strip().rstrip("/")
    username = data.get("username", "").strip()
    api_token = data.get("apiToken") or data.get("token")

    if not user_id:
        return jsonify({"error": "User authentication required"}), 401
    if not base_url:
        return jsonify({"error": "Jenkins URL is required"}), 400
    if not username:
        return jsonify({"error": "Jenkins username is required"}), 400
    if not api_token or not isinstance(api_token, str):
        return jsonify({"error": "Jenkins API token is required"}), 400

    logger.info("[JENKINS] Connecting user %s to %s", user_id, base_url)

    client = JenkinsClient(base_url=base_url, username=username, api_token=api_token)
    success, server_data, error = client.get_server_info()
    if not success:
        logger.warning("[JENKINS] Credential validation failed for user %s: %s", user_id, error)
        safe_errors = {
            "Invalid credentials. Check your username and API token.",
            "Forbidden. Insufficient permissions.",
            "Resource not found.",
            "Connection timeout. Verify the Jenkins URL is reachable.",
            "Cannot connect to Jenkins. Verify the URL and network access.",
        }
        msg = error if error in safe_errors else "Failed to validate Jenkins credentials"
        return jsonify({"error": msg}), 400

    version = server_data.get("version", "unknown") if server_data else "unknown"
    mode = server_data.get("mode", "unknown") if server_data else "unknown"

    token_payload = {
        "base_url": base_url,
        "username": username,
        "api_token": api_token,
        "version": version,
        "mode": mode,
    }

    try:
        store_tokens_in_db(user_id, token_payload, JENKINS_PROVIDER)
        logger.info("[JENKINS] Stored credentials for user %s (url=%s)", user_id, base_url)
    except Exception:
        logger.exception("[JENKINS] Failed to store credentials for user %s", user_id)
        return jsonify({"error": "Failed to store Jenkins credentials"}), 500

    return jsonify({
        "success": True,
        "baseUrl": base_url,
        "username": username,
        "server": {"version": version, "mode": mode},
    })


@jenkins_bp.route("/status", methods=["GET", "OPTIONS"])
def status():
    """Check whether Jenkins is connected and return summary dashboard data."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    creds = _get_stored_jenkins_credentials(user_id)
    if not creds:
        return jsonify({"connected": False})

    # Extract display-safe fields before passing creds to the client builder
    stored_base_url = creds.get("base_url", "")
    stored_username = creds.get("username", "")
    stored_version = creds.get("version")

    client = _build_client(creds)
    if not client:
        return jsonify({"connected": False})

    success, data, error = client.get_server_info()
    if not success:
        logger.warning("[JENKINS] Status check failed for user %s: %s", user_id, error)
        return jsonify({"connected": False, "error": "Failed to validate stored Jenkins credentials"})

    jobs = data.get("jobs", []) if data else []
    job_count = len(jobs)

    job_health = {"healthy": 0, "unstable": 0, "failing": 0, "disabled": 0, "other": 0}
    for job in jobs:
        color = (job.get("color") or "").lower().replace("_anime", "")
        if color == "blue":
            job_health["healthy"] += 1
        elif color == "yellow":
            job_health["unstable"] += 1
        elif color == "red":
            job_health["failing"] += 1
        elif color in ("disabled", "notbuilt"):
            job_health["disabled"] += 1
        else:
            job_health["other"] += 1

    queue_size = 0
    try:
        q_ok, q_data, _ = client.get_queue()
        if q_ok and q_data:
            queue_size = len(q_data.get("items", []))
    except Exception:
        pass

    nodes_online = 0
    nodes_offline = 0
    total_executors = 0
    busy_executors = 0
    try:
        n_ok, n_data, _ = client.list_nodes()
        if n_ok:
            for node in n_data:
                if node.get("offline"):
                    nodes_offline += 1
                else:
                    nodes_online += 1
                    total_executors += node.get("numExecutors", 0)
                    if not node.get("idle", True):
                        busy_executors += node.get("numExecutors", 0)
    except Exception:
        pass

    return jsonify({
        "connected": True,
        "baseUrl": stored_base_url,
        "username": stored_username,
        "server": {
            "version": stored_version,
            "mode": data.get("mode") if data else None,
            "numExecutors": data.get("numExecutors") if data else None,
        },
        "summary": {
            "jobCount": job_count,
            "jobHealth": job_health,
            "queueSize": queue_size,
            "nodesOnline": nodes_online,
            "nodesOffline": nodes_offline,
            "totalExecutors": total_executors,
            "busyExecutors": busy_executors,
        },
    })


@jenkins_bp.route("/disconnect", methods=["POST", "DELETE", "OPTIONS"])
def disconnect():
    """Disconnect Jenkins by removing stored credentials."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    try:
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM user_tokens WHERE user_id = %s AND provider = %s",
                (user_id, JENKINS_PROVIDER),
            )
            conn.commit()
            deleted = cursor.rowcount

        logger.info("[JENKINS] Disconnected user %s (deleted %d token rows)", user_id, deleted)
        return jsonify({"success": True, "message": "Jenkins disconnected successfully"})
    except Exception as exc:
        logger.exception("[JENKINS] Failed to disconnect user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to disconnect Jenkins"}), 500
