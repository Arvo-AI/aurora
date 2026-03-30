"""Prometheus integration routes.

Handles connection setup, Alertmanager webhook ingestion, active alert
polling, status checks, and disconnect.
"""

import hashlib
import hmac
import logging
import os
import secrets

from flask import Blueprint, jsonify, request

from connectors.prometheus_connector.api_client import PrometheusClient, PrometheusAPIError
from utils.db.connection_pool import db_pool
from utils.web.cors_utils import create_cors_response
from utils.auth.stateless_auth import get_user_id_from_request
from utils.auth.token_management import get_token_data, store_tokens_in_db

logger = logging.getLogger(__name__)

prometheus_bp = Blueprint("prometheus", __name__)


def _get_stored_credentials(user_id: str) -> dict | None:
    try:
        return get_token_data(user_id, "prometheus")
    except Exception:
        logger.exception("Failed to retrieve Prometheus credentials for user %s", user_id)
        return None


@prometheus_bp.route("/connect", methods=["POST", "OPTIONS"])
def connect():
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    data = request.get_json(force=True, silent=True) or {}
    base_url = (data.get("baseUrl") or "").strip()
    api_token = (data.get("apiToken") or "").strip() or None

    if not base_url:
        return jsonify({"error": "baseUrl is required"}), 400

    logger.info("[PROMETHEUS] Connecting user %s to %s", user_id, base_url)

    client = PrometheusClient(base_url, api_token)
    try:
        validation = client.validate()
    except PrometheusAPIError as exc:
        logger.error("[PROMETHEUS] Validation failed for user %s: %s", user_id, exc)
        return jsonify({"error": str(exc)}), 502

    webhook_secret = secrets.token_hex(32)
    try:
        store_tokens_in_db(
            user_id,
            {
                "base_url": base_url,
                "api_token": api_token,
                "webhook_secret": webhook_secret,
                "version": validation.get("version"),
            },
            "prometheus",
            subscription_name="prometheus",
            subscription_id=base_url,
        )
    except Exception as exc:
        logger.exception("[PROMETHEUS] Failed to store credentials for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to store Prometheus credentials"}), 500

    return jsonify({
        "success": True,
        "connected": True,
        "version": validation.get("version"),
        "baseUrl": base_url,
    })


@prometheus_bp.route("/status", methods=["GET", "OPTIONS"])
def status():
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    creds = _get_stored_credentials(user_id)
    if not creds or not creds.get("base_url"):
        return jsonify({"connected": False})

    return jsonify({
        "connected": True,
        "baseUrl": creds.get("base_url"),
        "version": creds.get("version"),
    })


@prometheus_bp.route("/disconnect", methods=["POST", "DELETE", "OPTIONS"])
def disconnect():
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
                (user_id, "prometheus"),
            )
            conn.commit()
        return jsonify({"success": True, "message": "Prometheus disconnected successfully"})
    except Exception as exc:
        logger.exception("[PROMETHEUS] Failed to disconnect user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to disconnect Prometheus"}), 500


# --- Active alert polling ---


@prometheus_bp.route("/alerts", methods=["GET", "OPTIONS"])
def get_alerts():
    """Fetch current firing alerts directly from user's Prometheus instance."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    creds = _get_stored_credentials(user_id)
    if not creds or not creds.get("base_url"):
        return jsonify({"error": "Prometheus not connected"}), 404

    client = PrometheusClient(creds["base_url"], creds.get("api_token"))
    try:
        alerts = client.get_alerts()
        return jsonify({"alerts": alerts, "count": len(alerts)})
    except PrometheusAPIError as exc:
        logger.error("[PROMETHEUS] Failed to fetch alerts for user %s: %s", user_id, exc)
        return jsonify({"error": str(exc)}), 502


@prometheus_bp.route("/targets", methods=["GET", "OPTIONS"])
def get_targets():
    """Fetch scrape target status from user's Prometheus instance."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    creds = _get_stored_credentials(user_id)
    if not creds or not creds.get("base_url"):
        return jsonify({"error": "Prometheus not connected"}), 404

    client = PrometheusClient(creds["base_url"], creds.get("api_token"))
    try:
        targets = client.get_targets()
        return jsonify(targets)
    except PrometheusAPIError as exc:
        logger.error("[PROMETHEUS] Failed to fetch targets for user %s: %s", user_id, exc)
        return jsonify({"error": str(exc)}), 502


@prometheus_bp.route("/query", methods=["POST", "OPTIONS"])
def query():
    """Execute a PromQL query against user's Prometheus instance."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    creds = _get_stored_credentials(user_id)
    if not creds or not creds.get("base_url"):
        return jsonify({"error": "Prometheus not connected"}), 404

    data = request.get_json(force=True, silent=True) or {}
    promql = (data.get("query") or "").strip()
    if not promql:
        return jsonify({"error": "query is required"}), 400

    client = PrometheusClient(creds["base_url"], creds.get("api_token"))
    try:
        result = client.query(promql)
        return jsonify(result)
    except PrometheusAPIError as exc:
        logger.error("[PROMETHEUS] Query failed for user %s: %s", user_id, exc)
        return jsonify({"error": str(exc)}), 502


# --- Alertmanager webhook ---


def _verify_webhook_user(user_id: str) -> bool:
    """Verify the user_id has Prometheus credentials stored."""
    if not user_id or len(user_id) > 255:
        return False
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT 1 FROM user_tokens WHERE user_id = %s AND provider = %s LIMIT 1",
                    (user_id, "prometheus"),
                )
                return cursor.fetchone() is not None
    except Exception as e:
        logger.warning("[PROMETHEUS] Webhook user verification failed: %s", e)
        return False


@prometheus_bp.route("/webhook/<user_id>", methods=["POST", "OPTIONS"])
def webhook(user_id: str):
    """Receive webhook events from Prometheus Alertmanager."""
    if request.method == "OPTIONS":
        return create_cors_response()

    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    if not _verify_webhook_user(user_id):
        logger.warning("[PROMETHEUS] Webhook rejected: invalid or unconfigured user_id %s", user_id[:50])
        return jsonify({"error": "Invalid webhook configuration"}), 403

    try:
        creds = get_token_data(user_id, "prometheus")
    except Exception as exc:
        logger.error("[PROMETHEUS] Failed to retrieve credentials for webhook user %s: %s", user_id, exc)
        return jsonify({"error": "Internal error processing webhook"}), 500
    if not creds:
        logger.warning("[PROMETHEUS] Webhook received for user %s with no connection", user_id)
        return jsonify({"error": "Prometheus not connected for this user"}), 404

    webhook_secret = creds.get("webhook_secret")
    signature = request.headers.get("X-Aurora-Signature", "")

    if webhook_secret:
        if not signature:
            logger.warning("[PROMETHEUS] Webhook rejected: missing X-Aurora-Signature for user %s", user_id[:50])
            return jsonify({"error": "Missing X-Aurora-Signature header"}), 401
        expected = hmac.new(webhook_secret.encode(), request.get_data(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            logger.warning("[PROMETHEUS] Webhook rejected: invalid signature for user %s", user_id[:50])
            return jsonify({"error": "Invalid webhook signature"}), 401

    payload = request.get_json(silent=True) or {}
    logger.info("[PROMETHEUS] Received webhook for user %s (%d alerts)", user_id, len(payload.get("alerts", [])))

    _REDACTED_HEADERS = {"authorization", "cookie", "set-cookie", "proxy-authorization", "x-api-key"}
    sanitized_headers = {
        k: ("<REDACTED>" if k.lower() in _REDACTED_HEADERS or "token" in k.lower() or "secret" in k.lower() else v)
        for k, v in request.headers
    }

    try:
        from routes.prometheus.tasks import process_prometheus_webhook
        process_prometheus_webhook.delay(payload, {"headers": sanitized_headers, "remote_addr": request.remote_addr}, user_id)
        return jsonify({"received": True})
    except Exception:
        logger.exception("[PROMETHEUS] Failed to enqueue webhook event for user %s", user_id)
        return jsonify({"error": "Failed to process webhook"}), 503


@prometheus_bp.route("/webhook-url", methods=["GET", "OPTIONS"])
def get_webhook_url():
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    ngrok_url = os.getenv("NGROK_URL", "").rstrip("/")
    backend_url = os.getenv("NEXT_PUBLIC_BACKEND_URL", "").rstrip("/")
    base_url = ngrok_url if ngrok_url and backend_url.startswith("http://localhost") else backend_url
    if not base_url:
        base_url = request.host_url.rstrip("/")

    return jsonify({
        "webhookUrl": f"{base_url}/prometheus/webhook/{user_id}",
        "signatureHeader": "X-Aurora-Signature",
        "signatureAlgorithm": "HMAC-SHA256 of request body using your webhook secret",
        "instructions": [
            "1. Open your Alertmanager configuration (alertmanager.yml)",
            "2. Add a webhook receiver with the URL above",
            "3. Example receiver config:",
            "   receivers:",
            "     - name: 'aurora'",
            "       webhook_configs:",
            "         - url: '<webhook_url>'",
            "4. Route alerts to this receiver in your routing tree",
            "5. Reload Alertmanager: kill -HUP $(pidof alertmanager)",
        ],
    })
