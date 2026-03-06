import logging
import os
import secrets
from typing import Any, Dict, Optional

from flask import Blueprint, jsonify, request

from connectors.spinnaker_connector.client import (
    SpinnakerClient,
    SpinnakerAPIError,
    get_spinnaker_client,
    get_spinnaker_client_for_user,
    invalidate_spinnaker_client,
)
from utils.db.connection_pool import db_pool
from utils.web.cors_utils import create_cors_response
from utils.web.webhook_signature import SIGNATURE_HEADER, verify_webhook_signature
from utils.auth.stateless_auth import get_user_id_from_request
from utils.auth.token_management import get_token_data, store_tokens_in_db

logger = logging.getLogger(__name__)

spinnaker_bp = Blueprint("spinnaker", __name__)

SPINNAKER_PROVIDER = "spinnaker"


def _get_stored_credentials(user_id: str) -> Optional[Dict[str, Any]]:
    try:
        return get_token_data(user_id, SPINNAKER_PROVIDER)
    except Exception as exc:
        logger.error("Failed to retrieve Spinnaker credentials for user %s: %s", user_id, exc)
        return None


def _get_cached_client(user_id: str) -> Optional["SpinnakerClient"]:
    """Get a cached SpinnakerClient from stored credentials."""
    return get_spinnaker_client_for_user(user_id)


# ------------------------------------------------------------------
# Connect / Status / Disconnect
# ------------------------------------------------------------------


@spinnaker_bp.route("/connect", methods=["POST", "OPTIONS"])
def connect():
    """Validate and store Spinnaker credentials (token or x509)."""
    if request.method == "OPTIONS":
        return create_cors_response()

    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    auth_type = data.get("authType", "token").strip()
    base_url = data.get("baseUrl", "").strip().rstrip("/")

    if not base_url:
        return jsonify({"error": "Spinnaker Gate URL is required"}), 400

    # Build kwargs depending on auth type
    if auth_type == "x509":
        cert_pem = data.get("certPem")
        key_pem = data.get("keyPem")
        ca_bundle_pem = data.get("caBundlePem")
        if not cert_pem or not key_pem:
            return jsonify({"error": "Certificate and key PEM files are required for X.509 auth"}), 400
        client_kwargs = {
            "cert_pem": cert_pem,
            "key_pem": key_pem,
            "ca_bundle_pem": ca_bundle_pem,
        }
    else:
        username = data.get("username", "").strip()
        password = data.get("password") or data.get("token", "")
        if not username or not password:
            return jsonify({"error": "Username and password/token are required"}), 400
        client_kwargs = {
            "username": username,
            "password": password,
        }

    logger.info("[SPINNAKER] Connecting user %s to %s (auth=%s)", user_id, base_url, auth_type)

    try:
        client = get_spinnaker_client(
            user_id=user_id,
            base_url=base_url,
            auth_type=auth_type,
            **client_kwargs,
        )
    except SpinnakerAPIError as e:
        logger.warning("[SPINNAKER] Credential validation failed for user %s: %s", user_id, e)
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.warning("[SPINNAKER] Connection failed for user %s: %s", user_id, e)
        return jsonify({"error": "Failed to connect to Spinnaker. Verify the URL and credentials."}), 400

    # Fetch apps and accounts for the response
    try:
        credentials = client.get_credentials()
        applications = client.list_applications()
    except Exception:
        credentials = []
        applications = []

    cloud_accounts = [c.get("name", "") for c in credentials if isinstance(c, dict)]

    token_payload = {
        "base_url": base_url,
        "auth_type": auth_type,
        "webhook_secret": secrets.token_hex(32),
        **client_kwargs,
    }

    try:
        store_tokens_in_db(user_id, token_payload, SPINNAKER_PROVIDER)
        logger.info("[SPINNAKER] Stored credentials for user %s (url=%s)", user_id, base_url)
    except Exception:
        logger.exception("[SPINNAKER] Failed to store credentials for user %s", user_id)
        return jsonify({"error": "Failed to store Spinnaker credentials"}), 500

    return jsonify({
        "connected": True,
        "baseUrl": base_url,
        "authType": auth_type,
        "applications": len(applications),
        "cloudAccounts": cloud_accounts,
    })


@spinnaker_bp.route("/status", methods=["GET", "OPTIONS"])
def status():
    """Check whether Spinnaker is connected and return summary data."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    creds = _get_stored_credentials(user_id)
    if not creds:
        return jsonify({"connected": False})

    client = _get_cached_client(user_id)
    if not client:
        return jsonify({"connected": False})

    try:
        credentials = client.get_credentials()
        applications = client.list_applications()
    except Exception as e:
        logger.warning("[SPINNAKER] Status check failed for user %s: %s", user_id, e)
        return jsonify({"connected": False, "error": "Failed to validate stored Spinnaker credentials"})

    cloud_accounts = [c.get("name", "") for c in credentials if isinstance(c, dict)]

    return jsonify({
        "connected": True,
        "baseUrl": creds.get("base_url", ""),
        "authType": creds.get("auth_type", "token"),
        "applications": len(applications),
        "cloudAccounts": cloud_accounts,
    })


@spinnaker_bp.route("/disconnect", methods=["POST", "DELETE", "OPTIONS"])
def disconnect():
    """Disconnect Spinnaker by removing stored credentials."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    try:
        invalidate_spinnaker_client(user_id)
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM user_tokens WHERE user_id = %s AND provider = %s",
                (user_id, SPINNAKER_PROVIDER),
            )
            conn.commit()
            deleted = cursor.rowcount

        logger.info("[SPINNAKER] Disconnected user %s (deleted %d token rows)", user_id, deleted)
        return jsonify({"success": True, "message": "Spinnaker disconnected successfully"})
    except Exception as exc:
        logger.exception("[SPINNAKER] Failed to disconnect user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to disconnect Spinnaker"}), 500


# ------------------------------------------------------------------
# Proxy endpoints: applications, pipelines, health
# ------------------------------------------------------------------


@spinnaker_bp.route("/applications", methods=["GET", "OPTIONS"])
def list_applications():
    """List Spinnaker applications."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    client = _get_cached_client(user_id)
    if not client:
        return jsonify({"error": "Spinnaker not connected"}), 400

    try:
        apps = client.list_applications()
        return jsonify({"applications": apps})
    except SpinnakerAPIError as e:
        return jsonify({"error": str(e)}), 502


@spinnaker_bp.route("/applications/<app>/pipelines", methods=["GET", "OPTIONS"])
def list_pipelines(app: str):
    """List pipeline executions for an application."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    client = _get_cached_client(user_id)
    if not client:
        return jsonify({"error": "Spinnaker not connected"}), 400

    limit = min(max(request.args.get("limit", 25, type=int), 1), 100)
    statuses = request.args.get("statuses")

    try:
        executions = client.list_pipeline_executions(app, limit=limit, statuses=statuses)
        return jsonify({"executions": executions})
    except SpinnakerAPIError as e:
        return jsonify({"error": str(e)}), 502


@spinnaker_bp.route("/applications/<app>/pipeline-configs", methods=["GET", "OPTIONS"])
def list_pipeline_configs(app: str):
    """List pipeline definitions for an application."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    client = _get_cached_client(user_id)
    if not client:
        return jsonify({"error": "Spinnaker not connected"}), 400

    try:
        configs = client.list_pipeline_configs(app)
        return jsonify({"pipelineConfigs": configs})
    except SpinnakerAPIError as e:
        return jsonify({"error": str(e)}), 502


@spinnaker_bp.route("/applications/<app>/pipelines/<name>/trigger", methods=["POST", "OPTIONS"])
def trigger_pipeline(app: str, name: str):
    """Trigger a named pipeline for an application."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    client = _get_cached_client(user_id)
    if not client:
        return jsonify({"error": "Spinnaker not connected"}), 400

    data = request.get_json(silent=True) or {}
    parameters = data.get("parameters")

    try:
        result = client.trigger_pipeline(app, name, parameters)
        return jsonify({"triggered": True, "result": result})
    except SpinnakerAPIError as e:
        return jsonify({"error": str(e)}), 502


@spinnaker_bp.route("/applications/<app>/health", methods=["GET", "OPTIONS"])
def application_health(app: str):
    """Get cluster + server group health for an application."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    client = _get_cached_client(user_id)
    if not client:
        return jsonify({"error": "Spinnaker not connected"}), 400

    try:
        clusters = client.list_clusters(app)
        return jsonify({"application": app, "clusters": clusters})
    except SpinnakerAPIError as e:
        return jsonify({"error": str(e)}), 502


# ------------------------------------------------------------------
# Webhook: receive deployment events from Spinnaker Echo
# ------------------------------------------------------------------


@spinnaker_bp.route("/webhook/<user_id>", methods=["POST", "OPTIONS"])
def deployment_webhook(user_id: str):
    """Receive a deployment event webhook from Spinnaker Echo.

    Security: validates per-user HMAC-SHA256 signature via X-Aurora-Signature header.
    """
    if request.method == "OPTIONS":
        return create_cors_response()

    if not user_id or len(user_id) > 255:
        return jsonify({"error": "user_id is required"}), 400

    creds = _get_stored_credentials(user_id)
    if not creds:
        logger.warning("[SPINNAKER] Webhook rejected: invalid or unconfigured user_id %s", user_id[:50])
        return jsonify({"error": "Invalid webhook configuration"}), 403

    webhook_secret = creds.get("webhook_secret")
    signature = request.headers.get(SIGNATURE_HEADER, "")

    if webhook_secret:
        if not signature:
            logger.warning("[SPINNAKER] Webhook rejected: missing %s for user %s", SIGNATURE_HEADER, user_id[:50])
            return jsonify({"error": f"Missing {SIGNATURE_HEADER} header"}), 401
        if not verify_webhook_signature(request.get_data(), signature, webhook_secret):
            logger.warning("[SPINNAKER] Webhook rejected: invalid signature for user %s", user_id[:50])
            return jsonify({"error": "Invalid webhook signature"}), 401

    payload = request.get_json(silent=True) or {}

    if not isinstance(payload, dict):
        return jsonify({"error": "Invalid payload format"}), 400

    logger.info(
        "[SPINNAKER] Received deployment webhook for user %s: app=%s pipeline=%s status=%s",
        user_id,
        payload.get("application", "unknown"),
        payload.get("pipeline", payload.get("pipeline_name", "unknown")),
        payload.get("status", payload.get("execution", {}).get("status", "unknown") if isinstance(payload.get("execution"), dict) else "unknown"),
    )

    from routes.spinnaker.tasks import process_spinnaker_deployment

    process_spinnaker_deployment.delay(payload, user_id)

    return jsonify({"received": True})


@spinnaker_bp.route("/webhook-url", methods=["GET", "OPTIONS"])
def get_webhook_url():
    """Return the webhook URL and Spinnaker Echo config snippets."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    backend_url = os.getenv("NEXT_PUBLIC_BACKEND_URL", "").rstrip("/")
    if not backend_url:
        backend_url = request.host_url.rstrip("/")

    webhook_url = f"{backend_url}/spinnaker/webhook/{user_id}"

    creds = _get_stored_credentials(user_id) or {}
    webhook_secret = creds.get("webhook_secret", "")

    echo_config = f"""# Add to your Spinnaker Echo configuration (echo-local.yml):
rest:
  enabled: true
  endpoints:
    - wrap: false
      url: "{webhook_url}"
      headers:
        Content-Type: application/json
      template: |-
        {{"application": "{{{{execution.application}}}}","pipeline": "{{{{execution.name}}}}","pipeline_name": "{{{{execution.name}}}}","execution_id": "{{{{execution.id}}}}","status": "{{{{execution.status}}}}","trigger_type": "{{{{execution.trigger.type}}}}","trigger_user": "{{{{execution.trigger.user}}}}","start_time": "{{{{execution.startTime}}}}","end_time": "{{{{execution.endTime}}}}"}}"""

    return jsonify({
        "webhookUrl": webhook_url,
        "echoConfig": echo_config,
        "instructions": [
            "1. Add the Echo notification config to your Spinnaker deployment (echo-local.yml)",
            "2. Restart the Echo service to pick up the new configuration",
            "3. Aurora will receive pipeline events and correlate them with incidents",
            "4. Failed pipelines will automatically trigger Root Cause Analysis",
        ],
    })


# ------------------------------------------------------------------
# Deployments: list stored events
# ------------------------------------------------------------------


@spinnaker_bp.route("/deployments", methods=["GET", "OPTIONS"])
def list_deployments():
    """List recent Spinnaker deployment events for the authenticated user."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    limit = min(max(request.args.get("limit", 20, type=int), 1), 100)
    offset = max(request.args.get("offset", 0, type=int), 0)
    app_filter = request.args.get("application")
    if app_filter:
        app_filter = app_filter[:255]

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                if app_filter:
                    cursor.execute(
                        """SELECT id, application, pipeline_name, execution_id, status,
                                  trigger_type, trigger_user, start_time, end_time, duration_ms,
                                  received_at
                           FROM spinnaker_deployment_events
                           WHERE user_id = %s AND application = %s
                           ORDER BY received_at DESC
                           LIMIT %s OFFSET %s""",
                        (user_id, app_filter, limit, offset),
                    )
                else:
                    cursor.execute(
                        """SELECT id, application, pipeline_name, execution_id, status,
                                  trigger_type, trigger_user, start_time, end_time, duration_ms,
                                  received_at
                           FROM spinnaker_deployment_events
                           WHERE user_id = %s
                           ORDER BY received_at DESC
                           LIMIT %s OFFSET %s""",
                        (user_id, limit, offset),
                    )
                rows = cursor.fetchall()

                cursor.execute(
                    "SELECT COUNT(*) FROM spinnaker_deployment_events WHERE user_id = %s"
                    + (" AND application = %s" if app_filter else ""),
                    (user_id, app_filter) if app_filter else (user_id,),
                )
                total = cursor.fetchone()[0]

        deployments = []
        for r in rows:
            deployments.append({
                "id": r[0],
                "application": r[1],
                "pipelineName": r[2],
                "executionId": r[3],
                "status": r[4],
                "triggerType": r[5],
                "triggerUser": r[6],
                "startTime": (r[7].isoformat() + "Z") if r[7] else None,
                "endTime": (r[8].isoformat() + "Z") if r[8] else None,
                "durationMs": r[9],
                "receivedAt": (r[10].isoformat() + "Z") if r[10] else None,
            })

        return jsonify({"deployments": deployments, "total": total, "limit": limit, "offset": offset})
    except Exception:
        logger.exception("[SPINNAKER] Failed to list deployments for user %s", user_id)
        return jsonify({"error": "Failed to list deployments"}), 500


# ------------------------------------------------------------------
# RCA settings: toggle automatic RCA on deployment failures
# ------------------------------------------------------------------
from routes.ci_shared import register_rca_settings_routes
register_rca_settings_routes(spinnaker_bp, "spinnaker", "spinnaker_rca_enabled")
