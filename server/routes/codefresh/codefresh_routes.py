"""Codefresh connector routes.

Codefresh uses API key auth (Authorization header) instead of Basic Auth,
so this is a standalone blueprint rather than a JenkinsClient wrapper.
"""

import logging
import os
import secrets
from typing import Any, Dict, Optional

from flask import Blueprint, jsonify, request

from connectors.codefresh_connector.api_client import CodefreshClient
from utils.db.connection_pool import db_pool
from utils.web.cors_utils import create_cors_response
from utils.web.webhook_signature import SIGNATURE_HEADER, verify_webhook_signature
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request
from utils.secrets.secret_ref_utils import delete_user_secret

logger = logging.getLogger(__name__)

codefresh_bp = Blueprint("codefresh", __name__)

CODEFRESH_PROVIDER = "codefresh"


def _get_stored_codefresh_credentials(user_id: str) -> Optional[Dict[str, Any]]:
    try:
        return get_token_data(user_id, CODEFRESH_PROVIDER)
    except Exception:
        logger.error("Failed to retrieve Codefresh credentials for user %s", user_id)
        return None


def _build_client(creds: Dict[str, Any]) -> Optional[CodefreshClient]:
    base_url = creds.get("base_url")
    api_token = creds.get("api_token")
    if not base_url or not api_token:
        return None
    return CodefreshClient(base_url=base_url, api_token=api_token)


@codefresh_bp.route("/connect", methods=["POST", "OPTIONS"])
@require_permission("connectors", "write")
def connect(user_id):
    """Validate and store Codefresh credentials."""
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    base_url = data.get("baseUrl", "").strip().rstrip("/")
    api_token = data.get("apiToken") or data.get("token")

    if not base_url:
        return jsonify({"error": "Codefresh URL is required"}), 400
    if not api_token or not isinstance(api_token, str):
        return jsonify({"error": "Codefresh API key is required"}), 400

    logger.info("[CODEFRESH] Connecting user %s to %s", user_id, base_url)

    client = CodefreshClient(base_url=base_url, api_token=api_token)
    success, data_resp, error = client.validate_credentials()
    if not success:
        logger.warning("[CODEFRESH] Credential validation failed for user %s", user_id)
        safe_errors = {
            "Invalid API key. Check your Codefresh API token.",
            "Forbidden. Insufficient permissions.",
            "Resource not found.",
            "Connection timeout. Verify the Codefresh URL is reachable.",
            "Cannot connect to Codefresh. Verify the URL and network access.",
        }
        msg = error if error in safe_errors else "Failed to validate Codefresh credentials"
        return jsonify({"error": msg}), 400

    token_payload = {
        "base_url": base_url,
        "api_token": api_token,
        "webhook_secret": secrets.token_hex(32),
    }

    try:
        store_tokens_in_db(user_id, token_payload, CODEFRESH_PROVIDER)
        logger.info("[CODEFRESH] Stored credentials for user %s (url=%s)", user_id, base_url)
    except Exception:
        logger.exception("[CODEFRESH] Failed to store credentials for user %s", user_id)
        return jsonify({"error": "Failed to store Codefresh credentials"}), 500

    return jsonify({
        "success": True,
        "baseUrl": base_url,
    })


@codefresh_bp.route("/status", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def status(user_id):
    """Check whether Codefresh is connected and return summary dashboard data."""
    creds = _get_stored_codefresh_credentials(user_id)
    if not creds:
        return jsonify({"connected": False})

    stored_base_url = creds.get("base_url", "")

    client = _build_client(creds)
    if not client:
        return jsonify({"connected": False})

    success, _, error = client.validate_credentials()
    if not success:
        logger.warning("[CODEFRESH] Status check failed for user %s", user_id)
        return jsonify({"connected": False, "error": "Failed to validate stored Codefresh credentials"})

    pipeline_count = 0
    build_health = {"healthy": 0, "unstable": 0, "failing": 0, "disabled": 0, "other": 0}

    try:
        p_ok, p_data, _ = client.list_pipelines(limit=100)
        if p_ok and p_data:
            docs = p_data.get("docs", []) if isinstance(p_data, dict) else []
            pipeline_count = len(docs)
    except Exception:
        logger.exception("[CODEFRESH] Failed to fetch pipelines for user %s", user_id)

    try:
        b_ok, b_data, _ = client.list_builds(limit=50)
        if b_ok and b_data:
            builds = b_data if isinstance(b_data, list) else b_data.get("workflows", []) if isinstance(b_data, dict) else []
            for build in builds:
                build_status = (build.get("status") or "").lower()
                if build_status in ("success",):
                    build_health["healthy"] += 1
                elif build_status in ("error", "failure"):
                    build_health["failing"] += 1
                elif build_status in ("terminated", "stopped"):
                    build_health["unstable"] += 1
                elif build_status in ("pending", "running"):
                    build_health["other"] += 1
                else:
                    build_health["other"] += 1
    except Exception:
        logger.exception("[CODEFRESH] Failed to fetch builds for user %s", user_id)

    return jsonify({
        "connected": True,
        "baseUrl": stored_base_url,
        "server": None,
        "summary": {
            "jobCount": pipeline_count,
            "jobHealth": build_health,
            "queueSize": 0,
            "nodesOnline": 0,
            "nodesOffline": 0,
            "totalExecutors": 0,
            "busyExecutors": 0,
        },
    })


@codefresh_bp.route("/disconnect", methods=["POST", "DELETE", "OPTIONS"])
@require_permission("connectors", "write")
def disconnect(user_id):
    """Disconnect Codefresh by removing stored credentials."""
    try:
        success, deleted = delete_user_secret(user_id, CODEFRESH_PROVIDER)
        if not success:
            logger.warning("[CODEFRESH] Failed to clean up secrets during disconnect")
            return jsonify({"success": False, "error": "Failed to delete stored credentials"}), 500

        logger.info("[CODEFRESH] Disconnected provider (deleted %d token rows)", deleted)
        return jsonify({"success": True, "message": "Codefresh disconnected successfully", "deleted": deleted})
    except Exception:
        logger.exception("[CODEFRESH] Failed to disconnect provider")
        return jsonify({"error": "Failed to disconnect Codefresh"}), 500


# ------------------------------------------------------------------
# Webhook: receive deployment events from codefresh.yml steps
# ------------------------------------------------------------------

def _get_webhook_secret(user_id: str) -> Optional[str]:
    """Retrieve the stored webhook secret for HMAC validation."""
    creds = _get_stored_codefresh_credentials(user_id)
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
                    (user_id, CODEFRESH_PROVIDER),
                )
                return cursor.fetchone() is not None
    except Exception as e:
        logger.warning("[CODEFRESH] Webhook user verification failed: %s", e)
        return False


@codefresh_bp.route("/webhook/<user_id>", methods=["POST", "OPTIONS"])
def deployment_webhook(user_id: str):
    """Receive a deployment event webhook from a Codefresh pipeline.

    Security: validates per-user HMAC-SHA256 signature via X-Aurora-Signature header.
    """
    if request.method == "OPTIONS":
        return create_cors_response()

    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    if not _verify_webhook_user(user_id):
        logger.warning("[CODEFRESH] Webhook rejected: invalid or unconfigured user_id %s", user_id[:50])
        return jsonify({"error": "Invalid webhook configuration"}), 403

    webhook_secret = _get_webhook_secret(user_id)
    signature = request.headers.get(SIGNATURE_HEADER, "")

    if webhook_secret:
        if not signature:
            logger.warning("[CODEFRESH] Webhook rejected: missing %s for user %s", SIGNATURE_HEADER, user_id[:50])
            return jsonify({"error": f"Missing {SIGNATURE_HEADER} header"}), 401
        if not verify_webhook_signature(request.get_data(), signature, webhook_secret):
            logger.warning("[CODEFRESH] Webhook rejected: invalid signature for user %s", user_id[:50])
            return jsonify({"error": "Invalid webhook signature"}), 401

    payload = request.get_json(silent=True) or {}

    if not isinstance(payload, dict):
        return jsonify({"error": "Invalid payload format"}), 400

    if not payload.get("result") and not payload.get("build_number"):
        return jsonify({"error": "Payload must include at least 'result' or 'build_number'"}), 400

    logger.info(
        "[CODEFRESH] Received deployment webhook for user %s: service=%s result=%s",
        user_id,
        payload.get("service") or payload.get("job_name", "unknown"),
        payload.get("result", "unknown"),
    )

    # Codefresh build IDs are strings — normalise before the shared task
    if payload.get("build_number") is not None:
        payload["build_number"] = str(payload["build_number"])

    from routes.jenkins.tasks import process_jenkins_deployment
    process_jenkins_deployment.apply_async(args=[payload, user_id, "codefresh"])

    return jsonify({"received": True})


@codefresh_bp.route("/webhook-url", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def get_webhook_url(user_id):
    """Return the webhook URL and codefresh.yml snippets for the authenticated user."""
    backend_url = os.getenv("NEXT_PUBLIC_BACKEND_URL", "").rstrip("/")
    if not backend_url:
        backend_url = request.host_url.rstrip("/")

    webhook_url = f"{backend_url}/codefresh/webhook/{user_id}"

    creds = _get_stored_codefresh_credentials(user_id) or {}
    webhook_secret = creds.get("webhook_secret", "")

    codefresh_yml_curl = f'''deploy_notify:
  title: Notify Aurora
  type: freestyle
  stage: deploy
  arguments:
    image: curlimages/curl:latest
    commands:
      - |
        PAYLOAD='{{"service":"${{CF_PIPELINE_NAME}}","environment":"${{CF_STEP_NAME}}","result":"${{CF_BUILD_STATUS}}","build_number":"${{CF_BUILD_ID}}","build_url":"${{CF_BUILD_URL}}","git":{{"commit_sha":"${{CF_REVISION}}","branch":"${{CF_BRANCH}}","repository":"${{CF_REPO_NAME}}"}}}}'
        SIG=$(echo -n "$PAYLOAD" | openssl dgst -sha256 -hmac "{webhook_secret}" | awk '{{print $2}}')
        curl -sS -X POST "{webhook_url}" \\
          -H "Content-Type: application/json" \\
          -H "{SIGNATURE_HEADER}: $SIG" \\
          -d "$PAYLOAD"
  when:
    condition:
      all:
        always: "true"'''

    codefresh_yml_basic = f'''deploy_notify:
  title: Notify Aurora
  type: freestyle
  stage: deploy
  arguments:
    image: curlimages/curl:latest
    commands:
      - |
        curl -sS -X POST "{webhook_url}" \\
          -H "Content-Type: application/json" \\
          -d '{{"service":"${{CF_PIPELINE_NAME}}","result":"${{CF_BUILD_STATUS}}","build_number":"${{CF_BUILD_ID}}","build_url":"${{CF_BUILD_URL}}","git":{{"commit_sha":"${{CF_REVISION}}","branch":"${{CF_BRANCH}}"}}}}'
  when:
    condition:
      all:
        always: "true"'''

    return jsonify({
        "webhookUrl": webhook_url,
        "jenkinsfileBasic": codefresh_yml_basic,
        "jenkinsfileOtel": codefresh_yml_curl,
        "jenkinsfileCurl": codefresh_yml_curl,
        "instructions": [
            "1. Add the step snippet to your codefresh.yml pipeline definition",
            "2. The step runs after every build and sends the result to Aurora",
            "3. Aurora will receive deployment events and correlate them with incidents",
            "4. HMAC signing is built into the curl snippet for webhook security",
        ],
    })


@codefresh_bp.route("/deployments", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def list_deployments(user_id):
    """List recent Codefresh deployment events for the authenticated user."""
    org_id = get_org_id_from_request()
    limit = min(max(request.args.get("limit", 20, type=int), 1), 100)
    offset = max(request.args.get("offset", 0, type=int), 0)
    service_filter = request.args.get("service")
    if service_filter:
        service_filter = service_filter[:255]

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SET myapp.current_org_id = %s", (org_id,))

                if service_filter:
                    cursor.execute(
                        """SELECT id, service, environment, result, build_number, build_url,
                                  commit_sha, branch, repository, deployer, duration_ms,
                                  job_name, trace_id, received_at
                           FROM ci_deployment_events
                           WHERE org_id = %s AND provider = 'codefresh' AND service = %s
                           ORDER BY received_at DESC
                           LIMIT %s OFFSET %s""",
                        (org_id, service_filter, limit, offset),
                    )
                else:
                    cursor.execute(
                        """SELECT id, service, environment, result, build_number, build_url,
                                  commit_sha, branch, repository, deployer, duration_ms,
                                  job_name, trace_id, received_at
                           FROM ci_deployment_events
                           WHERE org_id = %s AND provider = 'codefresh'
                           ORDER BY received_at DESC
                           LIMIT %s OFFSET %s""",
                        (org_id, limit, offset),
                    )
                rows = cursor.fetchall()

                cursor.execute(
                    "SELECT COUNT(*) FROM ci_deployment_events WHERE org_id = %s AND provider = 'codefresh'"
                    + (" AND service = %s" if service_filter else ""),
                    (org_id, service_filter) if service_filter else (org_id,),
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
                "receivedAt": (r[13].isoformat() + "Z") if r[13] else None,
            })

        return jsonify({"deployments": deployments, "total": total, "limit": limit, "offset": offset})
    except Exception:
        logger.exception("[CODEFRESH] Failed to list deployments for user %s", user_id)
        return jsonify({"error": "Failed to list deployments"}), 500


# ------------------------------------------------------------------
# RCA settings: toggle automatic RCA on deployment failures
# ------------------------------------------------------------------
from routes.ci_shared import register_rca_settings_routes
register_rca_settings_routes(codefresh_bp, "codefresh", "codefresh_rca_enabled")
