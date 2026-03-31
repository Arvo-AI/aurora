import json
import logging
import os
import re
from typing import Any, Dict, Optional

import requests
from flask import Blueprint, jsonify, request

from routes.elasticsearch.tasks import process_elasticsearch_alert
from utils.db.connection_pool import db_pool
from utils.web.cors_utils import create_cors_response
from utils.logging.secure_logging import mask_credential_value
from utils.auth.stateless_auth import (
    get_user_id_from_request,
    get_user_preference,
    store_user_preference,
)
from utils.auth.token_management import get_token_data, store_tokens_in_db

ELASTICSEARCH_TIMEOUT = 15

logger = logging.getLogger(__name__)

elasticsearch_bp = Blueprint("elasticsearch", __name__)


class ElasticsearchAPIError(Exception):
    """Custom error for Elasticsearch API interactions."""


class ElasticsearchClient:
    """Client for interacting with Elasticsearch REST API."""

    def __init__(self, base_url: str, api_key: Optional[str] = None,
                 username: Optional[str] = None, password: Optional[str] = None):
        self.base_url = base_url
        self.api_key = api_key
        self.username = username
        self.password = password

    @staticmethod
    def normalize_base_url(raw_url: str) -> Optional[str]:
        """Normalize and validate Elasticsearch instance URL."""
        if not raw_url:
            return None

        url = raw_url.strip()
        if not url:
            return None

        if not re.match(r"^https?://", url, re.IGNORECASE):
            url = "https://" + url

        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(url)
        url = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))

        if not re.match(r"^https?://[A-Za-z0-9._-]+(:[0-9]{2,5})?$", url):
            return None

        return url

    @property
    def headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"ApiKey {self.api_key}"
        return headers

    @property
    def auth(self) -> Optional[tuple]:
        if self.username and self.password:
            return (self.username, self.password)
        return None

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.base_url}{path}"
        try:
            response = requests.request(
                method, url, headers=self.headers, auth=self.auth,
                timeout=ELASTICSEARCH_TIMEOUT, verify=False, **kwargs
            )
            response.raise_for_status()
            return response
        except requests.exceptions.Timeout as exc:
            logger.error(f"[ELASTICSEARCH] {method} {url} timeout: {exc}")
            raise ElasticsearchAPIError("Connection timed out. Check if Elasticsearch is reachable.") from exc
        except requests.exceptions.SSLError as exc:
            logger.error(f"[ELASTICSEARCH] {method} {url} SSL error: {exc}")
            raise ElasticsearchAPIError("SSL/TLS error. Check the instance URL protocol (https://).") from exc
        except requests.exceptions.ConnectionError as exc:
            logger.error(f"[ELASTICSEARCH] {method} {url} connection error: {exc}")
            error_str = str(exc).lower()
            if "name or service not known" in error_str or "nodename nor servname" in error_str:
                raise ElasticsearchAPIError("DNS resolution failed. Check the instance URL.") from exc
            elif "connection refused" in error_str:
                raise ElasticsearchAPIError("Connection refused. Ensure Elasticsearch is running and the port is accessible.") from exc
            else:
                raise ElasticsearchAPIError("Unable to connect. Ensure Aurora has network access to your Elasticsearch instance.") from exc
        except requests.HTTPError as exc:
            logger.error(f"[ELASTICSEARCH] {method} {url} failed: {exc}")
            raise ElasticsearchAPIError(str(exc)) from exc
        except requests.RequestException as exc:
            logger.error(f"[ELASTICSEARCH] {method} {url} error: {exc}")
            raise ElasticsearchAPIError("Unable to reach Elasticsearch instance") from exc

    def get_cluster_info(self) -> Dict[str, Any]:
        """Fetch cluster info (root endpoint) to validate connection."""
        return self._request("GET", "/").json()

    def get_cluster_health(self) -> Dict[str, Any]:
        """Fetch cluster health."""
        return self._request("GET", "/_cluster/health").json()

    def get_current_user(self) -> Optional[Dict[str, Any]]:
        """Fetch current user context via security API."""
        try:
            return self._request("GET", "/_security/_authenticate").json()
        except ElasticsearchAPIError:
            logger.debug("[ELASTICSEARCH] Unable to fetch current user (security may be disabled)", exc_info=True)
            return None


def _get_stored_elasticsearch_credentials(user_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve stored Elasticsearch credentials for a user."""
    try:
        return get_token_data(user_id, "elasticsearch")
    except Exception as exc:
        logger.error(f"Failed to retrieve Elasticsearch credentials for user {user_id}: {exc}")
        return None


def _build_client_from_creds(creds: Dict[str, Any]) -> ElasticsearchClient:
    """Build an ElasticsearchClient from stored credentials."""
    return ElasticsearchClient(
        base_url=creds["base_url"],
        api_key=creds.get("api_key"),
        username=creds.get("username"),
        password=creds.get("password"),
    )


@elasticsearch_bp.route("/connect", methods=["POST", "OPTIONS"])
def connect():
    """Store Elasticsearch credentials and validate connectivity."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    api_key = data.get("apiKey")
    username = data.get("username")
    password = data.get("password")
    raw_base_url = data.get("baseUrl")
    auth_method = data.get("authMethod", "apiKey")

    if auth_method == "apiKey":
        if not api_key or not isinstance(api_key, str):
            return jsonify({"error": "apiKey is required when using API key authentication"}), 400
    elif auth_method == "basic":
        if not username or not password:
            return jsonify({"error": "username and password are required for basic authentication"}), 400
    else:
        return jsonify({"error": "authMethod must be 'apiKey' or 'basic'"}), 400

    base_url = ElasticsearchClient.normalize_base_url(raw_base_url) if raw_base_url else None
    if not base_url:
        return jsonify({"error": "A valid Elasticsearch URL is required (e.g., https://your-instance:9200)"}), 400

    logger.info(f"[ELASTICSEARCH] Connecting user {user_id} to {base_url}")

    client = ElasticsearchClient(
        base_url=base_url,
        api_key=api_key if auth_method == "apiKey" else None,
        username=username if auth_method == "basic" else None,
        password=password if auth_method == "basic" else None,
    )

    try:
        cluster_info = client.get_cluster_info()
        cluster_health = client.get_cluster_health()
        user_context = client.get_current_user()
    except ElasticsearchAPIError as exc:
        logger.error(f"[ELASTICSEARCH] Connection validation failed for user {user_id}: {exc}")
        return jsonify({"error": "Failed to validate Elasticsearch credentials"}), 502

    cluster_name = cluster_info.get("cluster_name", "elasticsearch")
    version = cluster_info.get("version", {}).get("number")
    distribution = cluster_info.get("version", {}).get("distribution", "elasticsearch")
    health_status = cluster_health.get("status")

    authenticated_user = None
    if user_context:
        authenticated_user = user_context.get("username")

    token_payload = {
        "base_url": base_url,
        "auth_method": auth_method,
        "cluster_name": cluster_name,
        "version": version,
        "distribution": distribution,
    }
    if auth_method == "apiKey":
        token_payload["api_key"] = api_key
    else:
        token_payload["username"] = username
        token_payload["password"] = password

    if authenticated_user:
        token_payload["authenticated_user"] = authenticated_user

    try:
        store_tokens_in_db(user_id, token_payload, "elasticsearch")
        logger.info(f"[ELASTICSEARCH] Stored credentials for user {user_id} (cluster={cluster_name})")
    except Exception as exc:
        logger.exception(f"[ELASTICSEARCH] Failed to store credentials for user {user_id}: {exc}")
        return jsonify({"error": "Failed to store Elasticsearch credentials"}), 500

    return jsonify({
        "success": True,
        "cluster": {
            "name": cluster_name,
            "version": version,
            "distribution": distribution,
            "health": health_status,
        },
        "baseUrl": base_url,
        "username": authenticated_user,
    })


@elasticsearch_bp.route("/status", methods=["GET", "OPTIONS"])
def status():
    """Check Elasticsearch connection status."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    creds = _get_stored_elasticsearch_credentials(user_id)
    if not creds:
        return jsonify({"connected": False})

    base_url = creds.get("base_url")
    if not base_url:
        logger.warning(f"[ELASTICSEARCH] Incomplete credentials for user {user_id}")
        return jsonify({"connected": False})

    client = _build_client_from_creds(creds)

    try:
        cluster_info = client.get_cluster_info()
        cluster_health = client.get_cluster_health()
    except ElasticsearchAPIError as exc:
        logger.warning(f"[ELASTICSEARCH] Status check failed for user {user_id}: {exc}")
        return jsonify({"connected": False, "error": "Failed to validate stored Elasticsearch credentials"})

    return jsonify({
        "connected": True,
        "cluster": {
            "name": cluster_info.get("cluster_name"),
            "version": cluster_info.get("version", {}).get("number"),
            "distribution": cluster_info.get("version", {}).get("distribution", "elasticsearch"),
            "health": cluster_health.get("status"),
        },
        "baseUrl": base_url,
        "username": creds.get("authenticated_user"),
    })


@elasticsearch_bp.route("/disconnect", methods=["POST", "DELETE", "OPTIONS"])
def disconnect():
    """Disconnect Elasticsearch by removing stored credentials."""
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
                (user_id, "elasticsearch")
            )
            conn.commit()
            deleted_count = cursor.rowcount

        logger.info(f"[ELASTICSEARCH] Disconnected user {user_id} (deleted {deleted_count} token entries)")

        return jsonify({
            "success": True,
            "message": "Elasticsearch disconnected successfully"
        }), 200

    except Exception as exc:
        logger.exception(f"[ELASTICSEARCH] Failed to disconnect user {user_id}: {exc}")
        return jsonify({"error": "Failed to disconnect Elasticsearch"}), 500


@elasticsearch_bp.route("/alerts/webhook/<user_id>", methods=["POST", "OPTIONS"])
def alert_webhook(user_id: str):
    """Receive alert webhook from Elasticsearch Watcher or OpenSearch Alerting."""
    if request.method == "OPTIONS":
        return create_cors_response()

    if not user_id:
        logger.warning("[ELASTICSEARCH] Webhook received without user_id")
        return jsonify({"error": "user_id is required"}), 400

    creds = get_token_data(user_id, "elasticsearch")
    if not creds:
        logger.warning("[ELASTICSEARCH] Webhook received for user %s with no Elasticsearch connection", user_id)
        return jsonify({"error": "Elasticsearch not connected for this user"}), 404

    payload = request.get_json(silent=True) or {}
    logger.info("[ELASTICSEARCH] Received alert webhook for user %s: %s", user_id,
                payload.get("watch_id", payload.get("alert_name", "unknown")))

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

    process_elasticsearch_alert.delay(payload, metadata, user_id)

    return jsonify({"received": True})


@elasticsearch_bp.route("/alerts", methods=["GET", "OPTIONS"])
def get_alerts():
    """Fetch Elasticsearch alerts for the authenticated user."""
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
                    SELECT id, alert_id, alert_title, alert_state, watch_id,
                           query, result_count, severity, payload, received_at, created_at
                    FROM elasticsearch_alerts
                    WHERE user_id = %s AND alert_state = %s
                    ORDER BY received_at DESC
                    LIMIT %s OFFSET %s
                    """,
                    (user_id, state_filter, limit, offset)
                )
            else:
                cursor.execute(
                    """
                    SELECT id, alert_id, alert_title, alert_state, watch_id,
                           query, result_count, severity, payload, received_at, created_at
                    FROM elasticsearch_alerts
                    WHERE user_id = %s
                    ORDER BY received_at DESC
                    LIMIT %s OFFSET %s
                    """,
                    (user_id, limit, offset)
                )

            alerts = cursor.fetchall()

            if state_filter:
                cursor.execute(
                    "SELECT COUNT(*) FROM elasticsearch_alerts WHERE user_id = %s AND alert_state = %s",
                    (user_id, state_filter)
                )
            else:
                cursor.execute(
                    "SELECT COUNT(*) FROM elasticsearch_alerts WHERE user_id = %s",
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
                    "watchId": row[4],
                    "query": row[5],
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
        logger.exception("[ELASTICSEARCH] Failed to fetch alerts for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to fetch alerts"}), 500


@elasticsearch_bp.route("/alerts/webhook-url", methods=["GET", "OPTIONS"])
def get_webhook_url():
    """Get the webhook URL that should be configured in Elasticsearch Watcher."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    ngrok_url = os.getenv("NGROK_URL", "").rstrip("/")
    backend_url = os.getenv("NEXT_PUBLIC_BACKEND_URL", "").rstrip("/")

    if ngrok_url and backend_url.startswith("http://localhost"):
        base_url = ngrok_url
    else:
        base_url = backend_url

    webhook_url = f"{base_url}/elasticsearch/alerts/webhook/{user_id}"

    return jsonify({
        "webhookUrl": webhook_url,
        "instructions": [
            "1. Go to your Elasticsearch/Kibana instance",
            "2. Navigate to Stack Management -> Watcher (or Alerting for OpenSearch)",
            "3. Create or edit a watch",
            "4. Add a webhook action with the URL above",
            "5. Set the method to POST and content type to application/json",
            "6. Save the watch configuration",
            "7. Alerts will now send notifications to Aurora when triggered"
        ]
    })


@elasticsearch_bp.route("/rca-settings", methods=["GET", "OPTIONS"])
def get_rca_settings():
    """Get Elasticsearch RCA settings for the authenticated user."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    rca_enabled = get_user_preference(user_id, "elasticsearch_rca_enabled", default=False)

    return jsonify({
        "rcaEnabled": rca_enabled,
    })


@elasticsearch_bp.route("/rca-settings", methods=["PUT", "OPTIONS"])
def update_rca_settings():
    """Update Elasticsearch RCA settings for the authenticated user."""
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

    store_user_preference(user_id, "elasticsearch_rca_enabled", rca_enabled)
    logger.info(f"[ELASTICSEARCH] Updated RCA settings for user {user_id}: rcaEnabled={rca_enabled}")

    return jsonify({
        "success": True,
        "rcaEnabled": rca_enabled,
    })
