import json
import logging
import os
import re
from typing import Any, Dict, Optional

import requests
from flask import Blueprint, jsonify, request

from routes.splunk.tasks import process_splunk_alert
from utils.db.connection_pool import db_pool
from utils.web.cors_utils import create_cors_response
from utils.logging.secure_logging import mask_credential_value
from utils.auth.stateless_auth import (
    get_user_id_from_request,
    get_user_preference,
    store_user_preference,
)
from utils.auth.token_management import get_token_data, store_tokens_in_db
SPLUNK_TIMEOUT = 15

logger = logging.getLogger(__name__)

splunk_bp = Blueprint("splunk", __name__)


class SplunkAPIError(Exception):
    """Custom error for Splunk API interactions."""


class SplunkClient:
    """Client for interacting with Splunk REST API."""

    def __init__(self, base_url: str, api_token: str):
        self.base_url = base_url
        self.api_token = api_token

    @staticmethod
    def normalize_base_url(raw_url: str) -> Optional[str]:
        """Normalize and validate Splunk instance URL."""
        if not raw_url:
            return None

        url = raw_url.strip()
        if not url:
            return None

        if not re.match(r"^https?://", url, re.IGNORECASE):
            url = "https://" + url

        # Remove any trailing paths (like /en-US/ from web UI URLs)
        url = re.sub(r"(/en-[A-Z]{2})?(/app/.*)?(/services/.*)?$", "", url, flags=re.IGNORECASE)
        url = url.rstrip("/")

        # Validate URL format (allow various ports for Enterprise/Cloud)
        if not re.match(r"^https?://[A-Za-z0-9._-]+(:[0-9]{2,5})?$", url):
            return None

        return url

    @property
    def headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.base_url}{path}"
        try:
            # verify=False to support self-signed certs (common in Splunk Enterprise)
            response = requests.request(
                method, url, headers=self.headers, timeout=SPLUNK_TIMEOUT, verify=False, **kwargs
            )
            response.raise_for_status()
            return response
        except requests.exceptions.Timeout as exc:
            logger.error(f"[SPLUNK] {method} {url} timeout: {exc}")
            raise SplunkAPIError("Connection timed out. Check if Splunk is reachable and port 8089 is open.") from exc
        except requests.exceptions.SSLError as exc:
            # SSLError must come before ConnectionError (it's a subclass)
            logger.error(f"[SPLUNK] {method} {url} SSL error: {exc}")
            raise SplunkAPIError("SSL/TLS error. Check the instance URL protocol (https://).") from exc
        except requests.exceptions.ConnectionError as exc:
            logger.error(f"[SPLUNK] {method} {url} connection error: {exc}")
            error_str = str(exc).lower()
            if "name or service not known" in error_str or "nodename nor servname" in error_str:
                raise SplunkAPIError("DNS resolution failed. Check the instance URL.") from exc
            elif "connection refused" in error_str:
                raise SplunkAPIError("Connection refused. Ensure Splunk is running and port 8089 is accessible.") from exc
            else:
                raise SplunkAPIError("Unable to connect. Ensure Aurora has network access to your Splunk instance.") from exc
        except requests.HTTPError as exc:
            logger.error(f"[SPLUNK] {method} {url} failed: {exc}")
            raise SplunkAPIError(str(exc)) from exc
        except requests.RequestException as exc:
            logger.error(f"[SPLUNK] {method} {url} error: {exc}")
            raise SplunkAPIError("Unable to reach Splunk instance") from exc

    def get_server_info(self) -> Dict[str, Any]:
        """Fetch server info to validate connection."""
        return self._request("GET", "/services/server/info?output_mode=json").json()

    def get_current_user(self) -> Optional[Dict[str, Any]]:
        """Fetch current user context."""
        try:
            return self._request("GET", "/services/authentication/current-context?output_mode=json").json()
        except SplunkAPIError:
            logger.debug("[SPLUNK] Unable to fetch current user context", exc_info=True)
            return None


def _get_stored_splunk_credentials(user_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve stored Splunk credentials for a user."""
    try:
        return get_token_data(user_id, "splunk")
    except Exception as exc:
        logger.error(f"Failed to retrieve Splunk credentials for user {user_id}: {exc}")
        return None


@splunk_bp.route("/connect", methods=["POST", "OPTIONS"])
def connect():
    """Store Splunk API token and validate connectivity."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    api_token = data.get("apiToken")
    raw_base_url = data.get("baseUrl")

    if not api_token or not isinstance(api_token, str):
        return jsonify({"error": "apiToken is required"}), 400

    base_url = SplunkClient.normalize_base_url(raw_base_url) if raw_base_url else None
    if not base_url:
        return jsonify({"error": "A valid Splunk instance URL is required (e.g., https://your-instance.splunkcloud.com:8089)"}), 400

    masked_token = mask_credential_value(api_token)
    logger.info(f"[SPLUNK] Connecting user {user_id} to {base_url} (token={masked_token})")

    client = SplunkClient(base_url, api_token)

    try:
        server_info = client.get_server_info()
        user_context = client.get_current_user()
    except SplunkAPIError as exc:
        return jsonify({"error": str(exc)}), 502

    # Extract server details
    entry = server_info.get("entry", [{}])[0] if server_info.get("entry") else {}
    content = entry.get("content", {})
    server_name = content.get("serverName", "Splunk")
    version = content.get("version")
    instance_type = content.get("instance_type", "enterprise")

    # Extract user info
    user_entry = user_context.get("entry", [{}])[0] if user_context and user_context.get("entry") else {}
    user_content = user_entry.get("content", {})
    username = user_content.get("username")

    token_payload = {
        "api_token": api_token,
        "base_url": base_url,
        "server_name": server_name,
        "version": version,
        "instance_type": instance_type,
        "username": username,
    }

    try:
        store_tokens_in_db(user_id, token_payload, "splunk")
        logger.info(f"[SPLUNK] Stored credentials for user {user_id} (server={server_name})")
    except Exception as exc:
        logger.exception(f"[SPLUNK] Failed to store credentials for user {user_id}: {exc}")
        return jsonify({"error": "Failed to store Splunk credentials"}), 500

    return jsonify({
        "success": True,
        "server": {
            "name": server_name,
            "version": version,
            "instanceType": instance_type,
        },
        "baseUrl": base_url,
        "username": username,
    })


@splunk_bp.route("/status", methods=["GET", "OPTIONS"])
def status():
    """Check Splunk connection status."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    creds = _get_stored_splunk_credentials(user_id)
    if not creds:
        return jsonify({"connected": False})

    api_token = creds.get("api_token")
    base_url = creds.get("base_url")

    if not api_token or not base_url:
        logger.warning(f"[SPLUNK] Incomplete credentials for user {user_id}")
        return jsonify({"connected": False})

    client = SplunkClient(base_url, api_token)

    try:
        server_info = client.get_server_info()
        user_context = client.get_current_user()
    except SplunkAPIError as exc:
        logger.warning(f"[SPLUNK] Status check failed for user {user_id}: {exc}")
        return jsonify({"connected": False, "error": str(exc)})

    entry = server_info.get("entry", [{}])[0] if server_info.get("entry") else {}
    content = entry.get("content", {})

    return jsonify({
        "connected": True,
        "server": {
            "name": content.get("serverName"),
            "version": content.get("version"),
            "instanceType": content.get("instance_type", "enterprise"),
        },
        "baseUrl": base_url,
        "username": creds.get("username"),
    })


@splunk_bp.route("/disconnect", methods=["POST", "DELETE", "OPTIONS"])
def disconnect():
    """Disconnect Splunk by removing stored credentials."""
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
                (user_id, "splunk")
            )
            conn.commit()
            deleted_count = cursor.rowcount

        logger.info(f"[SPLUNK] Disconnected user {user_id} (deleted {deleted_count} token entries)")

        return jsonify({
            "success": True,
            "message": "Splunk disconnected successfully"
        }), 200

    except Exception as exc:
        logger.exception(f"[SPLUNK] Failed to disconnect user {user_id}: {exc}")
        return jsonify({"error": "Failed to disconnect Splunk"}), 500


@splunk_bp.route("/alerts/webhook/<user_id>", methods=["POST", "OPTIONS"])
def alert_webhook(user_id: str):
    """Receive alert webhook from Splunk for a specific user."""
    if request.method == "OPTIONS":
        return create_cors_response()

    if not user_id:
        logger.warning("[SPLUNK] Webhook received without user_id")
        return jsonify({"error": "user_id is required"}), 400

    # Check if user has Splunk connected
    creds = get_token_data(user_id, "splunk")
    if not creds:
        logger.warning("[SPLUNK] Webhook received for user %s with no Splunk connection", user_id)
        return jsonify({"error": "Splunk not connected for this user"}), 404

    payload = request.get_json(silent=True) or {}
    logger.info("[SPLUNK] Received alert webhook for user %s: %s", user_id, payload.get("search_name", "unknown"))

    # Sanitize headers - redact sensitive values
    sensitive_headers = {"authorization", "cookie", "set-cookie", "proxy-authorization", "x-api-key", "x-csrf-token"}
    sanitized_headers = {}
    for key, value in request.headers:
        key_lower = key.lower()
        if key_lower in sensitive_headers or "token" in key_lower or "secret" in key_lower:
            sanitized_headers[key] = "<REDACTED>"
        else:
            sanitized_headers[key] = value

    metadata = {
        "headers": sanitized_headers,
        "remote_addr": request.remote_addr,
    }

    process_splunk_alert.delay(payload, metadata, user_id)

    return jsonify({"received": True})


@splunk_bp.route("/alerts", methods=["GET", "OPTIONS"])
def get_alerts():
    """Fetch Splunk alerts for the authenticated user."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    limit = request.args.get("limit", 50, type=int)
    offset = request.args.get("offset", 0, type=int)
    state_filter = request.args.get("state")

    try:
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()

            if state_filter:
                cursor.execute(
                    """
                    SELECT id, alert_id, alert_title, alert_state, search_name,
                           search_query, result_count, severity, payload, received_at, created_at
                    FROM splunk_alerts
                    WHERE user_id = %s AND alert_state = %s
                    ORDER BY received_at DESC
                    LIMIT %s OFFSET %s
                    """,
                    (user_id, state_filter, limit, offset)
                )
            else:
                cursor.execute(
                    """
                    SELECT id, alert_id, alert_title, alert_state, search_name,
                           search_query, result_count, severity, payload, received_at, created_at
                    FROM splunk_alerts
                    WHERE user_id = %s
                    ORDER BY received_at DESC
                    LIMIT %s OFFSET %s
                    """,
                    (user_id, limit, offset)
                )

            alerts = cursor.fetchall()

            # Get total count
            if state_filter:
                cursor.execute(
                    "SELECT COUNT(*) FROM splunk_alerts WHERE user_id = %s AND alert_state = %s",
                    (user_id, state_filter)
                )
            else:
                cursor.execute(
                    "SELECT COUNT(*) FROM splunk_alerts WHERE user_id = %s",
                    (user_id,)
                )
            total_count = cursor.fetchone()[0]

        return jsonify({
            "alerts": [
                {
                    "id": row[0],
                    "alertId": row[1],
                    "title": row[2],
                    "state": row[3],
                    "searchName": row[4],
                    "searchQuery": row[5],
                    "resultCount": row[6],
                    "severity": row[7],
                    "payload": row[8],
                    "receivedAt": row[9].isoformat() if row[9] else None,
                    "createdAt": row[10].isoformat() if row[10] else None,
                }
                for row in alerts
            ],
            "total": total_count,
            "limit": limit,
            "offset": offset,
        })
    except Exception as exc:
        logger.exception("[SPLUNK] Failed to fetch alerts for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to fetch alerts"}), 500


@splunk_bp.route("/alerts/webhook-url", methods=["GET", "OPTIONS"])
def get_webhook_url():
    """Get the webhook URL that should be configured in Splunk."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    # Use ngrok URL for development if available, otherwise use backend URL
    ngrok_url = os.getenv("NGROK_URL", "").rstrip("/")
    backend_url = os.getenv("NEXT_PUBLIC_BACKEND_URL", "").rstrip("/")

    # For development, prefer ngrok URL if available
    if ngrok_url and backend_url.startswith("http://localhost"):
        base_url = ngrok_url
    else:
        base_url = backend_url

    webhook_url = f"{base_url}/splunk/alerts/webhook/{user_id}"

    return jsonify({
        "webhookUrl": webhook_url,
        "instructions": [
            "1. Go to your Splunk instance",
            "2. Navigate to Settings -> Searches, reports, and alerts",
            "3. Find your saved search/alert and click Edit -> Edit Alert",
            "4. Under 'Trigger Actions', click 'Add Actions' -> 'Webhook'",
            "5. Paste the webhook URL above",
            "6. Save the alert configuration",
            "7. The alert will now send notifications to Aurora when triggered"
        ]
    })


@splunk_bp.route("/rca-settings", methods=["GET", "OPTIONS"])
def get_rca_settings():
    """Get Splunk RCA settings for the authenticated user."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    rca_enabled = get_user_preference(user_id, "splunk_rca_enabled", default=False)

    return jsonify({
        "rcaEnabled": rca_enabled,
    })


@splunk_bp.route("/rca-settings", methods=["PUT", "OPTIONS"])
def update_rca_settings():
    """Update Splunk RCA settings for the authenticated user."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    rca_enabled = data.get("rcaEnabled", False)

    if not isinstance(rca_enabled, bool):
        return jsonify({"error": "rcaEnabled must be a boolean"}), 400

    store_user_preference(user_id, "splunk_rca_enabled", rca_enabled)
    logger.info(f"[SPLUNK] Updated RCA settings for user {user_id}: rcaEnabled={rca_enabled}")

    return jsonify({
        "success": True,
        "rcaEnabled": rca_enabled,
    })
