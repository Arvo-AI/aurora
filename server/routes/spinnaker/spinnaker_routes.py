import logging
import os
import secrets
import uuid
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
from utils.auth.stateless_auth import get_org_id_from_request, set_rls_context
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.auth.rbac_decorators import require_permission
from utils.log_sanitizer import sanitize
from utils.secrets.secret_ref_utils import delete_user_secret

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


@spinnaker_bp.route("/connect", methods=["POST"])
@require_permission("connectors", "write")
def connect(user_id):
    """Validate and store Spinnaker credentials (token or x509)."""
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    auth_type = data.get("authType", "token").strip()
    base_url = data.get("baseUrl", "").strip().rstrip("/")

    if not base_url:
        return jsonify({"error": "Spinnaker Gate URL is required"}), 400

    if not base_url.startswith(("http://", "https://")):
        return jsonify({"error": "Spinnaker Gate URL must start with http:// or https://"}), 400

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
        return jsonify({"error": "Credential validation failed. Check your Spinnaker URL and credentials."}), 400
    except Exception:
        logger.exception("[SPINNAKER] Connection failed for user %s", user_id)
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
@require_permission("connectors", "read")
def status(user_id):
    """Check whether Spinnaker is connected and return summary data."""
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


@spinnaker_bp.route("/disconnect", methods=["POST", "DELETE"])
@require_permission("connectors", "write")
def disconnect(user_id):
    """Disconnect Spinnaker by removing stored credentials."""
    try:
        invalidate_spinnaker_client(user_id)
        success, deleted = delete_user_secret(user_id, SPINNAKER_PROVIDER)
        if not success:
            logger.warning("[SPINNAKER] Failed to clean up secrets during disconnect")
            return jsonify({"success": False, "error": "Failed to delete stored credentials"}), 500

        logger.info("[SPINNAKER] Disconnected provider (deleted %d token rows)", deleted)
        return jsonify({"success": True, "message": "Spinnaker disconnected successfully", "deleted": deleted})
    except Exception as exc:
        logger.exception("[SPINNAKER] Failed to disconnect provider")
        return jsonify({"error": "Failed to disconnect Spinnaker"}), 500


# ------------------------------------------------------------------
# Proxy endpoints: applications, pipelines, health
# ------------------------------------------------------------------


@spinnaker_bp.route("/applications", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def list_applications(user_id):
    """List Spinnaker applications."""
    client = _get_cached_client(user_id)
    if not client:
        return jsonify({"error": "Spinnaker not connected"}), 400

    try:
        apps = client.list_applications()
        return jsonify({"applications": apps})
    except SpinnakerAPIError as e:
        logger.warning("[SPINNAKER] API error: %s", e)
        return jsonify({"error": "Spinnaker API request failed"}), 502


@spinnaker_bp.route("/applications/<app>/pipelines", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def list_pipelines(user_id, app: str):
    """List pipeline executions for an application."""
    client = _get_cached_client(user_id)
    if not client:
        return jsonify({"error": "Spinnaker not connected"}), 400

    limit = min(max(request.args.get("limit", 25, type=int), 1), 100)
    statuses = request.args.get("statuses")

    try:
        executions = client.list_pipeline_executions(app, limit=limit, statuses=statuses)
        return jsonify({"executions": executions})
    except SpinnakerAPIError as e:
        logger.warning("[SPINNAKER] API error: %s", e)
        return jsonify({"error": "Spinnaker API request failed"}), 502


@spinnaker_bp.route("/applications/<app>/pipeline-configs", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def list_pipeline_configs(user_id, app: str):
    """List pipeline definitions for an application."""
    client = _get_cached_client(user_id)
    if not client:
        return jsonify({"error": "Spinnaker not connected"}), 400

    try:
        configs = client.list_pipeline_configs(app)
        return jsonify({"pipelineConfigs": configs})
    except SpinnakerAPIError as e:
        logger.warning("[SPINNAKER] API error: %s", e)
        return jsonify({"error": "Spinnaker API request failed"}), 502


@spinnaker_bp.route("/applications/<app>/pipelines/<name>/trigger", methods=["POST"])
@require_permission("connectors", "write")
def trigger_pipeline(user_id, app: str, name: str):
    """Trigger a named pipeline for an application."""
    client = _get_cached_client(user_id)
    if not client:
        return jsonify({"error": "Spinnaker not connected"}), 400

    data = request.get_json(silent=True) or {}
    parameters = data.get("parameters")

    try:
        result = client.trigger_pipeline(app, name, parameters)
        return jsonify({"triggered": True, "result": result})
    except SpinnakerAPIError as e:
        logger.warning("[SPINNAKER] API error: %s", e)
        return jsonify({"error": "Spinnaker API request failed"}), 502


@spinnaker_bp.route("/applications/<app>/health", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def application_health(user_id, app: str):
    """Get cluster + server group health for an application."""
    client = _get_cached_client(user_id)
    if not client:
        return jsonify({"error": "Spinnaker not connected"}), 400

    try:
        clusters = client.list_clusters(app)
        return jsonify({"application": app, "clusters": clusters})
    except SpinnakerAPIError as e:
        logger.warning("[SPINNAKER] API error: %s", e)
        return jsonify({"error": "Spinnaker API request failed"}), 502


# ------------------------------------------------------------------
# Webhook: receive deployment events from Spinnaker Echo
# ------------------------------------------------------------------


@spinnaker_bp.route("/webhook/<user_id>", methods=["POST"], strict_slashes=False)
def deployment_webhook(user_id: str):
    """Receive a deployment event webhook from Spinnaker Echo.

    Security: validates per-user HMAC-SHA256 signature via X-Aurora-Signature header
    when present.  Echo does not support HMAC signing, so the signature check is
    only enforced when the header is actually provided.
    """
    if not user_id or len(user_id) > 255:
        return jsonify({"error": "user_id is required"}), 400

    try:
        uuid.UUID(user_id)
    except ValueError:
        return jsonify({"error": "Invalid user_id format"}), 400

    creds = _get_stored_credentials(user_id)
    if not creds:
        logger.warning("[SPINNAKER] Webhook rejected: invalid or unconfigured user_id %s", sanitize(user_id)[:50])
        return jsonify({"error": "Invalid webhook configuration"}), 403

    webhook_secret = creds.get("webhook_secret")
    signature = request.headers.get(SIGNATURE_HEADER, "")

    if webhook_secret and signature:
        if not verify_webhook_signature(request.get_data(), signature, webhook_secret):
            logger.warning("[SPINNAKER] Webhook rejected: invalid signature for user %s", sanitize(user_id)[:50])
            return jsonify({"error": "Invalid webhook signature"}), 401

    payload = request.get_json(silent=True) or {}

    if not isinstance(payload, dict):
        return jsonify({"error": "Invalid payload format"}), 400

    # Normalise raw Echo/Orca event format into the flat format the task expects.
    # Echo sends: { details: { type, application, ... }, content: { execution: { ... } } }
    content = payload.get("content", {})
    execution = content.get("execution", {}) if isinstance(content, dict) else {}
    if isinstance(execution, dict) and execution and "details" in payload:
        payload = {
            "application": execution.get("application") or payload.get("details", {}).get("application", ""),
            "pipeline": execution.get("name", ""),
            "pipeline_name": execution.get("name", ""),
            "execution_id": execution.get("id") or content.get("executionId", ""),
            "status": execution.get("status", ""),
            "trigger_type": execution.get("trigger", {}).get("type", "") if isinstance(execution.get("trigger"), dict) else "",
            "trigger_user": execution.get("trigger", {}).get("user", "") if isinstance(execution.get("trigger"), dict) else "",
            "start_time": execution.get("startTime") or execution.get("buildTime"),
            "end_time": execution.get("endTime"),
            "event_type": payload.get("details", {}).get("type", "pipeline"),
            "execution": execution,
        }

    logger.info(
        "[SPINNAKER] Received deployment webhook for user %s: app=%s pipeline=%s status=%s",
        sanitize(user_id),
        sanitize(payload.get("application", "unknown")),
        sanitize(payload.get("pipeline", payload.get("pipeline_name", "unknown"))),
        sanitize(payload.get("status", payload.get("execution", {}).get("status", "unknown") if isinstance(payload.get("execution"), dict) else "unknown")),
    )

    from routes.spinnaker.tasks import process_spinnaker_deployment

    process_spinnaker_deployment.delay(payload, user_id)

    return jsonify({"received": True})


@spinnaker_bp.route("/webhook-url", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def get_webhook_url(user_id):
    """Return the webhook URL and Spinnaker Echo config snippets."""
    backend_url = os.getenv("NEXT_PUBLIC_BACKEND_URL", "").rstrip("/")
    if not backend_url:
        backend_url = request.host_url.rstrip("/")

    webhook_url = f"{backend_url}/spinnaker/webhook/{user_id}"

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
@require_permission("connectors", "read")
def list_deployments(user_id):
    """List recent Spinnaker deployment events for the authenticated user."""
    org_id = get_org_id_from_request()

    limit = min(max(request.args.get("limit", 20, type=int), 1), 100)
    offset = max(request.args.get("offset", 0, type=int), 0)
    app_filter = request.args.get("application")
    if app_filter:
        app_filter = app_filter[:255]

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix="[SPINNAKER:deployments]")
                base_where = "WHERE user_id = %s"
                params: list = [user_id]
                if org_id:
                    base_where += " AND org_id = %s"
                    params.append(org_id)
                if app_filter:
                    base_where += " AND application = %s"
                    params.append(app_filter)

                cursor.execute(
                    f"""SELECT id, application, pipeline_name, execution_id, status,
                              trigger_type, trigger_user, start_time, end_time, duration_ms,
                              received_at
                       FROM spinnaker_deployment_events
                       {base_where}
                       ORDER BY received_at DESC
                       LIMIT %s OFFSET %s""",
                    (*params, limit, offset),
                )
                rows = cursor.fetchall()

                cursor.execute(
                    f"SELECT COUNT(*) FROM spinnaker_deployment_events {base_where}",
                    tuple(params),
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
