"""Flask routes for the Prometheus connector.

Provides endpoints for:
- Connecting/disconnecting Prometheus instances (multi-instance per org)
- Credential validation against the Prometheus HTTP API
- Webhook URL generation for Alertmanager notifications
- Querying Prometheus via API proxy
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from flask import Blueprint, jsonify, request

from connectors.prometheus_connector.alertmanager_client import AlertmanagerClient
from connectors.prometheus_connector.client import PrometheusClient, PrometheusAPIError
from connectors.prometheus_connector.base_client import build_auth_headers_from_creds
from routes.prometheus.tasks import process_prometheus_alert
from utils.db.connection_pool import db_pool
from utils.log_sanitizer import sanitize
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request, set_rls_context
from utils.secrets.secret_ref_utils import delete_user_secret

logger = logging.getLogger(__name__)

prometheus_bp = Blueprint("prometheus", __name__)

PROMETHEUS_TIMEOUT = 30


def _get_stored_prometheus_credentials(user_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve stored Prometheus credentials for a user (or their org)."""
    try:
        data = get_token_data(user_id, "prometheus")
        if data:
            return data

        org_id = get_org_id_from_request()
        if not org_id:
            return None

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix="[PROMETHEUS:get_creds]")
                cursor.execute(
                    "SELECT user_id FROM user_tokens WHERE org_id = %s AND provider = 'prometheus' AND is_active = TRUE AND secret_ref IS NOT NULL LIMIT 1",
                    (org_id,)
                )
                row = cursor.fetchone()

        if row:
            return get_token_data(row[0], "prometheus") or None

        return None
    except Exception as exc:
        logger.error("[PROMETHEUS] Failed to retrieve credentials for user %s: %s", user_id, exc)
        return None



# ------------------------------------------------------------------
# Connect / Status / Disconnect
# ------------------------------------------------------------------


@prometheus_bp.route("/connect", methods=["POST"])
@require_permission("connectors", "write")
def connect(user_id):
    """Store and validate Prometheus connection credentials."""
    payload = request.get_json(force=True, silent=True) or {}

    prometheus_url = payload.get("prometheusUrl")
    instance_label = payload.get("instanceLabel", "default")
    alertmanager_url = payload.get("alertmanagerUrl")
    auth_type = payload.get("authType", "none")
    username = payload.get("username")
    password = payload.get("password")
    bearer_token = payload.get("bearerToken")
    custom_headers = payload.get("customHeaders")
    verify_ssl = payload.get("verifySsl", True)

    if not prometheus_url:
        return jsonify({"error": "Prometheus URL is required"}), 400

    prometheus_url = prometheus_url.strip().rstrip("/")
    auth_type = auth_type.strip().lower() if auth_type else "none"
    if auth_type not in ("none", "basic", "bearer", "custom"):
        return jsonify({"error": "auth_type must be one of: none, basic, bearer, custom"}), 400

    # Validate auth fields
    if auth_type == "basic" and (not username or not password):
        return jsonify({"error": "Username and password are required for basic auth"}), 400
    if auth_type == "bearer" and not bearer_token:
        return jsonify({"error": "Bearer token is required for bearer auth"}), 400

    logger.info(
        "[PROMETHEUS] Connecting user %s url=%s auth=%s label=%s",
        sanitize(user_id), sanitize(prometheus_url), sanitize(auth_type), sanitize(instance_label),
    )

    try:
        client = PrometheusClient(
            prometheus_url=prometheus_url,
            auth_type=auth_type,
            username=username,
            password=password,
            bearer_token=bearer_token,
            custom_headers=custom_headers,
            verify_ssl=verify_ssl,
            timeout=PROMETHEUS_TIMEOUT,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        build_info = client.validate_connection()
    except PrometheusAPIError as exc:
        logger.warning("[PROMETHEUS] Validation failed for user %s: %s", user_id, exc)
        return jsonify({"error": f"Failed to connect to Prometheus: {exc}"}), 400
    except Exception as exc:
        logger.warning("[PROMETHEUS] Connection failed for user %s: %s", user_id, exc)
        return jsonify({"error": "Unable to reach Prometheus server. Check the URL and network connectivity."}), 400

    # Validate Alertmanager URL if provided
    if alertmanager_url:
        try:
            am_client = AlertmanagerClient(
                alertmanager_url=alertmanager_url.strip().rstrip("/"),
                auth_headers=build_auth_headers_from_creds({
                    "auth_type": auth_type,
                    "username": username,
                    "password": password,
                    "bearer_token": bearer_token,
                    "custom_headers": custom_headers,
                }),
                verify_ssl=verify_ssl,
            )
            am_client.validate_connection()
            logger.info("[PROMETHEUS] Alertmanager validated at %s for user %s", sanitize(alertmanager_url), sanitize(user_id))
        except Exception as exc:
            logger.warning("[PROMETHEUS] Alertmanager validation failed for user %s: %s", user_id, exc)
            return jsonify({"error": f"Prometheus connected, but cannot reach Alertmanager at {alertmanager_url}: {exc}"}), 400

    token_payload: Dict[str, Any] = {
        "prometheus_url": prometheus_url,
        "instance_label": instance_label,
        "alertmanager_url": alertmanager_url,
        "auth_type": auth_type,
        "verify_ssl": verify_ssl,
        "build_info": build_info,
        "validated_at": datetime.now(timezone.utc).isoformat(),
    }

    # Store auth credentials (sensitive fields)
    if auth_type == "basic":
        token_payload["username"] = username
        token_payload["password"] = password
    elif auth_type == "bearer":
        token_payload["bearer_token"] = bearer_token
    elif auth_type == "custom" and custom_headers:
        token_payload["custom_headers"] = custom_headers

    try:
        store_tokens_in_db(user_id, token_payload, "prometheus")
        logger.info("[PROMETHEUS] Stored credentials for user %s (url=%s)", sanitize(user_id), sanitize(prometheus_url))
    except Exception as exc:
        logger.exception("[PROMETHEUS] Failed to store credentials: %s", exc)
        return jsonify({"error": "Failed to store Prometheus credentials"}), 500

    return jsonify({
        "success": True,
        "prometheusUrl": prometheus_url,
        "instanceLabel": instance_label,
        "alertmanagerUrl": alertmanager_url,
        "authType": auth_type,
        "version": build_info.get("version", "unknown"),
        "backend": build_info.get("_backend", "prometheus"),
        "validated": True,
    })


@prometheus_bp.route("/status", methods=["GET"])
@require_permission("connectors", "read")
def status(user_id):
    """Check connection status by verifying stored credentials exist."""
    creds = _get_stored_prometheus_credentials(user_id)
    if not creds:
        return jsonify({"connected": False})

    prometheus_url = creds.get("prometheus_url")
    if not prometheus_url:
        return jsonify({"connected": False})

    return jsonify({
        "connected": True,
        "prometheusUrl": prometheus_url,
        "instanceLabel": creds.get("instance_label", "default"),
        "alertmanagerUrl": creds.get("alertmanager_url"),
        "authType": creds.get("auth_type", "none"),
        "version": creds.get("build_info", {}).get("version", "unknown") if isinstance(creds.get("build_info"), dict) else "unknown",
        "backend": creds.get("build_info", {}).get("_backend", "prometheus") if isinstance(creds.get("build_info"), dict) else "prometheus",
        "validatedAt": creds.get("validated_at"),
    })


@prometheus_bp.route("/disconnect", methods=["DELETE", "POST"])
@require_permission("connectors", "write")
def disconnect(user_id):
    """Remove stored Prometheus credentials and backing Vault secrets."""
    try:
        success, deleted = delete_user_secret(user_id, "prometheus")
        if not success:
            logger.warning("[PROMETHEUS] Failed to clean up secrets during disconnect")
            return jsonify({"success": False, "error": "Failed to delete stored credentials"}), 500

        logger.info("[PROMETHEUS] Disconnected user %s (deleted %d token rows)", user_id, deleted)
        return jsonify({
            "success": True,
            "message": "Prometheus disconnected successfully",
            "tokensDeleted": deleted,
        })
    except Exception as exc:
        logger.exception("[PROMETHEUS] Failed to disconnect user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to disconnect Prometheus"}), 500


# ------------------------------------------------------------------
# Webhook URL (for Alertmanager setup)
# ------------------------------------------------------------------


@prometheus_bp.route("/webhook-url", methods=["GET"])
@require_permission("connectors", "read")
def webhook_url(user_id):
    """Get the webhook URL to configure in Alertmanager."""
    ngrok_url = os.getenv("NGROK_URL", "").rstrip("/")
    backend_url = os.getenv("NEXT_PUBLIC_BACKEND_URL", "").rstrip("/")

    # Use ngrok for local dev, otherwise the public backend URL
    if ngrok_url and backend_url.startswith("http://localhost"):
        base_url = ngrok_url
    else:
        base_url = backend_url

    return jsonify({"webhookUrl": f"{base_url}/prometheus/webhook/{user_id}"})


# ------------------------------------------------------------------
# Webhook receiver (called by Alertmanager — no RBAC, authenticates via user_id in URL)
# ------------------------------------------------------------------


@prometheus_bp.route("/webhook/<user_id>", methods=["POST"])
def webhook(user_id: str):
    """Receive alert notifications from Alertmanager and enqueue processing."""
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    # Webhook is called by Alertmanager (no auth headers) — look up creds
    # via admin connection since RLS context can't be set from external request
    creds = get_token_data(user_id, "prometheus")
    if not creds:
        logger.warning("[PROMETHEUS] Webhook received for user %s with no connection", sanitize(user_id))
        return jsonify({"error": "Prometheus not connected for this user"}), 404

    payload = request.get_json(force=True, silent=True) or {}
    if not payload:
        return jsonify({"error": "Empty payload"}), 400

    metadata = {
        "headers": {k: v for k, v in request.headers if k.lower() not in ("authorization",)},
        "remote_addr": request.remote_addr,
    }

    # Alertmanager sends alerts as a batch
    alerts = payload.get("alerts", [])

    logger.info(
        "[PROMETHEUS][WEBHOOK] Received %d alert(s) for user %s",
        len(alerts), sanitize(user_id),
    )

    process_prometheus_alert.delay(payload, metadata, user_id)

    return jsonify({"accepted": True, "alertCount": len(alerts)}), 202
