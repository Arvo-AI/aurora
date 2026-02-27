"""CloudBees CI connector routes.

CloudBees CI uses the same REST API as Jenkins, so we reuse JenkinsClient
with a separate provider identifier for credential storage.
"""

import logging
import os
import secrets
from typing import Any, Dict, Optional

from flask import Blueprint, jsonify, request

from connectors.jenkins_connector.api_client import JenkinsClient
from utils.db.connection_pool import db_pool
from utils.web.cors_utils import create_cors_response
from utils.web.webhook_signature import SIGNATURE_HEADER, verify_webhook_signature
from utils.auth.stateless_auth import get_user_id_from_request
from utils.auth.token_management import get_token_data, store_tokens_in_db

logger = logging.getLogger(__name__)

cloudbees_bp = Blueprint("cloudbees", __name__)

CLOUDBEES_PROVIDER = "cloudbees"


def _get_stored_cloudbees_credentials(user_id: str) -> Optional[Dict[str, Any]]:
    try:
        return get_token_data(user_id, CLOUDBEES_PROVIDER)
    except Exception as exc:
        logger.error("Failed to retrieve CloudBees credentials for user %s: %s", user_id, exc)
        return None


def _build_client(creds: Dict[str, Any]) -> Optional[JenkinsClient]:
    base_url = creds.get("base_url")
    username = creds.get("username")
    api_token = creds.get("api_token")
    if not base_url or not username or not api_token:
        return None
    return JenkinsClient(base_url=base_url, username=username, api_token=api_token)


@cloudbees_bp.route("/connect", methods=["POST", "OPTIONS"])
def connect():
    """Validate and store CloudBees CI credentials."""
    if request.method == "OPTIONS":
        return create_cors_response()

    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    user_id = get_user_id_from_request()
    base_url = data.get("baseUrl", "").strip().rstrip("/")
    # Strip common Jenkins redirect paths that users may accidentally copy
    for suffix in ("/loginError", "/login", "/manage", "/configure", "/view/all"):
        if base_url.lower().endswith(suffix.lower()):
            base_url = base_url[: -len(suffix)]
            break
    username = data.get("username", "").strip()
    api_token = data.get("apiToken") or data.get("token")

    if not user_id:
        return jsonify({"error": "User authentication required"}), 401
    if not base_url:
        return jsonify({"error": "CloudBees CI URL is required"}), 400
    if not username:
        return jsonify({"error": "CloudBees CI username is required"}), 400
    if not api_token or not isinstance(api_token, str):
        return jsonify({"error": "CloudBees CI API token is required"}), 400

    logger.info("[CLOUDBEES] Connecting user %s to %s", user_id, base_url)

    client = JenkinsClient(base_url=base_url, username=username, api_token=api_token)
    success, server_data, error = client.get_server_info()
    if not success:
        logger.warning("[CLOUDBEES] Credential validation failed for user %s: %s", user_id, error)
        safe_errors = {
            "Invalid credentials. Check your username and API token.",
            "Forbidden. Insufficient permissions.",
            "Resource not found.",
            "Connection timeout. Verify the Jenkins URL is reachable.",
            "Cannot connect to Jenkins. Verify the URL and network access.",
        }
        msg = error if error in safe_errors else "Failed to validate CloudBees CI credentials"
        return jsonify({"error": msg}), 400

    version = server_data.get("version", "unknown") if server_data else "unknown"
    mode = server_data.get("mode", "unknown") if server_data else "unknown"

    token_payload = {
        "base_url": base_url,
        "username": username,
        "api_token": api_token,
        "version": version,
        "mode": mode,
        "webhook_secret": secrets.token_hex(32),
    }

    try:
        store_tokens_in_db(user_id, token_payload, CLOUDBEES_PROVIDER)
        logger.info("[CLOUDBEES] Stored credentials for user %s (url=%s)", user_id, base_url)
    except Exception:
        logger.exception("[CLOUDBEES] Failed to store credentials for user %s", user_id)
        return jsonify({"error": "Failed to store CloudBees CI credentials"}), 500

    return jsonify({
        "success": True,
        "baseUrl": base_url,
        "username": username,
        "server": {"version": version, "mode": mode},
    })


@cloudbees_bp.route("/status", methods=["GET", "OPTIONS"])
def status():
    """Check whether CloudBees CI is connected and return summary dashboard data."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    creds = _get_stored_cloudbees_credentials(user_id)
    if not creds:
        return jsonify({"connected": False})

    stored_base_url = creds.get("base_url", "")
    stored_username = creds.get("username", "")
    stored_version = creds.get("version")

    client = _build_client(creds)
    if not client:
        return jsonify({"connected": False})

    success, data, error = client.get_server_info()
    if not success:
        logger.warning("[CLOUDBEES] Status check failed for user %s", user_id)
        return jsonify({"connected": False, "error": "Failed to validate stored CloudBees CI credentials"})

    include_extras = request.args.get("full", "").lower() in ("true", "1", "yes")

    jobs: list = []
    job_count = 0
    job_health = {"healthy": 0, "unstable": 0, "failing": 0, "disabled": 0, "other": 0}
    try:
        j_ok, j_data, _ = client.list_jobs()
        if j_ok:
            jobs = j_data
            job_count = len(jobs)
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
    except Exception:
        logger.exception("[CLOUDBEES] Failed to fetch job list for user %s", user_id)

    queue_size = 0
    nodes_online = 0
    nodes_offline = 0
    total_executors = 0
    busy_executors = 0

    if include_extras:
        try:
            q_ok, q_data, _ = client.get_queue()
            if q_ok and q_data:
                queue_size = len(q_data.get("items", []))
        except Exception:
            logger.exception("[CLOUDBEES] Failed to fetch queue information for user %s", user_id)

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
            logger.exception("[CLOUDBEES] Failed to fetch node information for user %s", user_id)

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


@cloudbees_bp.route("/disconnect", methods=["POST", "DELETE", "OPTIONS"])
def disconnect():
    """Disconnect CloudBees CI by removing stored credentials."""
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
                (user_id, CLOUDBEES_PROVIDER),
            )
            conn.commit()
            deleted = cursor.rowcount

        logger.info("[CLOUDBEES] Disconnected user %s (deleted %d token rows)", user_id, deleted)
        return jsonify({"success": True, "message": "CloudBees CI disconnected successfully"})
    except Exception as exc:
        logger.exception("[CLOUDBEES] Failed to disconnect user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to disconnect CloudBees CI"}), 500


# ------------------------------------------------------------------
# Webhook: receive deployment events from Jenkinsfile post blocks
# ------------------------------------------------------------------

def _get_webhook_secret(user_id: str) -> Optional[str]:
    """Retrieve the stored webhook secret for HMAC validation."""
    creds = _get_stored_cloudbees_credentials(user_id)
    if not creds:
        return None
    return creds.get("webhook_secret")


def _verify_webhook_user(user_id: str) -> bool:
    """Verify the user_id exists in the database to prevent arbitrary data injection."""
    if not user_id or len(user_id) > 255:
        return False
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT 1 FROM user_tokens WHERE user_id = %s AND provider = %s LIMIT 1",
                    (user_id, CLOUDBEES_PROVIDER),
                )
                return cursor.fetchone() is not None
    except Exception as e:
        logger.warning("[CLOUDBEES] Webhook user verification failed: %s", e)
        return False


@cloudbees_bp.route("/webhook/<user_id>", methods=["POST", "OPTIONS"])
def deployment_webhook(user_id: str):
    """Receive a deployment event webhook from a CloudBees CI pipeline.

    Security: validates per-user HMAC-SHA256 signature via X-Aurora-Signature header.
    Falls back to user verification only when no webhook secret is configured (pre-upgrade).
    """
    if request.method == "OPTIONS":
        return create_cors_response()

    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    if not _verify_webhook_user(user_id):
        logger.warning("[CLOUDBEES] Webhook rejected: invalid or unconfigured user_id %s", user_id[:50])
        return jsonify({"error": "Invalid webhook configuration"}), 403

    webhook_secret = _get_webhook_secret(user_id)
    signature = request.headers.get(SIGNATURE_HEADER, "")

    if webhook_secret:
        if not signature:
            logger.warning("[CLOUDBEES] Webhook rejected: missing %s for user %s", SIGNATURE_HEADER, user_id[:50])
            return jsonify({"error": f"Missing {SIGNATURE_HEADER} header"}), 401
        if not verify_webhook_signature(request.get_data(), signature, webhook_secret):
            logger.warning("[CLOUDBEES] Webhook rejected: invalid signature for user %s", user_id[:50])
            return jsonify({"error": "Invalid webhook signature"}), 401

    payload = request.get_json(silent=True) or {}

    if not isinstance(payload, dict):
        return jsonify({"error": "Invalid payload format"}), 400
    if not payload.get("result") and not payload.get("build_number"):
        return jsonify({"error": "Payload must include at least 'result' or 'build_number'"}), 400

    logger.info(
        "[CLOUDBEES] Received deployment webhook for user %s: service=%s result=%s",
        user_id,
        payload.get("service") or payload.get("job_name", "unknown"),
        payload.get("result", "unknown"),
    )

    from routes.jenkins.tasks import process_jenkins_deployment

    process_jenkins_deployment.apply_async(args=[payload, user_id, "cloudbees"])

    return jsonify({"received": True})


@cloudbees_bp.route("/webhook-url", methods=["GET", "OPTIONS"])
def get_webhook_url():
    """Return the webhook URL and Jenkinsfile snippets for the authenticated user."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    backend_url = os.getenv("BACKEND_URL", "").rstrip("/")
    if not backend_url:
        backend_url = request.host_url.rstrip("/")

    webhook_url = f"{backend_url}/cloudbees/webhook/{user_id}"

    creds = _get_stored_cloudbees_credentials(user_id) or {}
    webhook_secret = creds.get("webhook_secret", "")

    jenkinsfile_basic = f'''post {{
  always {{
    script {{
      def payload = """{{"service":"${{env.JOB_NAME}}","environment":"${{params.ENVIRONMENT ?: 'production'}}","result":"${{currentBuild.currentResult}}","build_number":${{env.BUILD_NUMBER}},"build_url":"${{env.BUILD_URL}}","git":{{"commit_sha":"${{env.GIT_COMMIT}}","branch":"${{env.GIT_BRANCH}}"}}}}"""
      def mac = javax.crypto.Mac.getInstance("HmacSHA256")
      mac.init(new javax.crypto.spec.SecretKeySpec("{webhook_secret}".bytes, "HmacSHA256"))
      def sig = mac.doFinal(payload.bytes).encodeHex().toString()
      httpRequest(
        url: "{webhook_url}",
        httpMode: 'POST',
        contentType: 'APPLICATION_JSON',
        customHeaders: [[name: '{SIGNATURE_HEADER}', value: sig]],
        requestBody: payload
      )
    }}
  }}
}}'''

    jenkinsfile_otel = f'''post {{
  always {{
    script {{
      def payload = """{{"service":"${{env.JOB_NAME}}","environment":"${{params.ENVIRONMENT ?: 'production'}}","result":"${{currentBuild.currentResult}}","build_number":${{env.BUILD_NUMBER}},"build_url":"${{env.BUILD_URL}}","git":{{"commit_sha":"${{env.GIT_COMMIT}}","branch":"${{env.GIT_BRANCH}}"}},"trace_id":"${{env.TRACEPARENT?.split('-')?.getAt(1) ?: ''}}","span_id":"${{env.TRACEPARENT?.split('-')?.getAt(2) ?: ''}}"}}"""
      def mac = javax.crypto.Mac.getInstance("HmacSHA256")
      mac.init(new javax.crypto.spec.SecretKeySpec("{webhook_secret}".bytes, "HmacSHA256"))
      def sig = mac.doFinal(payload.bytes).encodeHex().toString()
      httpRequest(
        url: "{webhook_url}",
        httpMode: 'POST',
        contentType: 'APPLICATION_JSON',
        customHeaders: [[name: '{SIGNATURE_HEADER}', value: sig]],
        requestBody: payload
      )
    }}
  }}
}}'''

    jenkinsfile_curl = f'''post {{
  always {{
    script {{
      env.BUILD_RESULT = currentBuild.currentResult ?: 'UNKNOWN'
    }}
    sh \'\'\'
      PAYLOAD='{{"service":"\'\"$JOB_NAME\"\'","result":"\'\"$BUILD_RESULT\"\'","build_number":\'$BUILD_NUMBER\',"build_url":"\'\"$BUILD_URL\"\'","git":{{"commit_sha":"\'\"$GIT_COMMIT\"\'","branch":"\'\"$GIT_BRANCH\"\'"}}}}'
      SIG=$(echo -n "$PAYLOAD" | openssl dgst -sha256 -hmac "{webhook_secret}" | awk \'{{print $2}}\')
      curl -sS -X POST "{webhook_url}" \\
        -H "Content-Type: application/json" \\
        -H "{SIGNATURE_HEADER}: $SIG" \\
        -d "$PAYLOAD"
    \'\'\'
  }}
}}'''

    return jsonify({
        "webhookUrl": webhook_url,
        "jenkinsfileBasic": jenkinsfile_basic,
        "jenkinsfileOtel": jenkinsfile_otel,
        "jenkinsfileCurl": jenkinsfile_curl,
        "instructions": [
            "1. Add the post block snippet to your Jenkinsfile (HMAC signing is built in)",
            "2. Ensure the HTTP Request Plugin is installed on your CloudBees CI instance",
            "3. Aurora will receive deployment events and correlate them with incidents",
            "4. (Optional) Install the OpenTelemetry plugin for W3C Trace Context propagation",
        ],
    })


@cloudbees_bp.route("/deployments", methods=["GET", "OPTIONS"])
def list_deployments():
    """List recent CloudBees CI deployment events for the authenticated user."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    limit = min(max(request.args.get("limit", 20, type=int), 1), 100)
    offset = max(request.args.get("offset", 0, type=int), 0)
    service_filter = request.args.get("service")
    if service_filter:
        service_filter = service_filter[:255]

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                if service_filter:
                    cursor.execute(
                        """SELECT id, service, environment, result, build_number, build_url,
                                  commit_sha, branch, repository, deployer, duration_ms,
                                  job_name, trace_id, received_at
                           FROM jenkins_deployment_events
                           WHERE user_id = %s AND service = %s
                           ORDER BY received_at DESC
                           LIMIT %s OFFSET %s""",
                        (user_id, service_filter, limit, offset),
                    )
                else:
                    cursor.execute(
                        """SELECT id, service, environment, result, build_number, build_url,
                                  commit_sha, branch, repository, deployer, duration_ms,
                                  job_name, trace_id, received_at
                           FROM jenkins_deployment_events
                           WHERE user_id = %s
                           ORDER BY received_at DESC
                           LIMIT %s OFFSET %s""",
                        (user_id, limit, offset),
                    )
                rows = cursor.fetchall()

                cursor.execute(
                    "SELECT COUNT(*) FROM jenkins_deployment_events WHERE user_id = %s"
                    + (" AND service = %s" if service_filter else ""),
                    (user_id, service_filter) if service_filter else (user_id,),
                )
                total = cursor.fetchone()[0]

        deployments = []
        for r in rows:
            deployments.append({
                "id": r[0],
                "service": r[1],
                "environment": r[2],
                "result": r[3],
                "buildNumber": r[4],
                "buildUrl": r[5],
                "commitSha": r[6],
                "branch": r[7],
                "repository": r[8],
                "deployer": r[9],
                "durationMs": r[10],
                "jobName": r[11],
                "traceId": r[12],
                "receivedAt": r[13].isoformat() if r[13] else None,
            })

        return jsonify({"deployments": deployments, "total": total, "limit": limit, "offset": offset})
    except Exception as exc:
        logger.exception("[CLOUDBEES] Failed to list deployments for user %s", user_id)
        return jsonify({"error": "Failed to list deployments"}), 500
