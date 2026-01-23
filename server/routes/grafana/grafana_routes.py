import logging
import os
import re
from typing import Any, Dict, Optional

import requests
from flask import Blueprint, jsonify, request

from routes.grafana.tasks import process_grafana_alert
from utils.db.connection_pool import db_pool
from utils.web.cors_utils import create_cors_response
from utils.logging.secure_logging import mask_credential_value
from utils.auth.stateless_auth import get_user_id_from_request
from utils.auth.token_management import get_token_data, store_tokens_in_db
GRAFANA_TIMEOUT = 15

logger = logging.getLogger(__name__)

grafana_bp = Blueprint("grafana", __name__)


class GrafanaAPIError(Exception):
    """Custom error for Grafana API interactions."""


class GrafanaClient:
    def __init__(self, base_url: str, api_token: str):
        self.base_url = base_url
        self.api_token = api_token

    @staticmethod
    def normalize_base_url(raw_url: str) -> Optional[str]:
        if not raw_url:
            return None

        url = raw_url.strip()
        if not url:
            return None

        if not re.match(r"^https?://", url, re.IGNORECASE):
            url = "https://" + url

        url = url.rstrip("/")

        if not re.match(r"^https://[A-Za-z0-9._-]+(:[0-9]{2,5})?(\/.*)?$", url):
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
            response = requests.request(method, url, headers=self.headers, timeout=GRAFANA_TIMEOUT, **kwargs)
            response.raise_for_status()
            return response
        except requests.HTTPError as exc:
            logger.error(f"[GRAFANA] {method} {url} failed: {exc}")
            raise GrafanaAPIError(str(exc)) from exc
        except requests.RequestException as exc:
            logger.error(f"[GRAFANA] {method} {url} error: {exc}")
            raise GrafanaAPIError("Unable to reach Grafana") from exc

    def get_org(self) -> Dict[str, Any]:
        return self._request("GET", "/api/org").json()

    def get_user(self) -> Optional[Dict[str, Any]]:
        try:
            return self._request("GET", "/api/user").json()
        except GrafanaAPIError:
            logger.debug("[GRAFANA] Unable to fetch user profile", exc_info=True)
            return None


def _get_stored_grafana_credentials(user_id: str) -> Optional[Dict[str, Any]]:
    try:
        return get_token_data(user_id, "grafana")
    except Exception as exc:
        logger.error(f"Failed to retrieve Grafana credentials for user {user_id}: {exc}")
        return None


@grafana_bp.route("/connect", methods=["POST", "OPTIONS"])
def connect():
    """Store Grafana API token and validate connectivity."""
    if request.method == "OPTIONS":
        return create_cors_response()

    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    user_id = data.get("userId") or get_user_id_from_request()
    api_token = data.get("apiToken") or data.get("token")
    raw_base_url = data.get("baseUrl")
    stack_slug = data.get("stackSlug")

    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    if not api_token or not isinstance(api_token, str):
        return jsonify({"error": "Grafana API token is required"}), 400

    base_url = GrafanaClient.normalize_base_url(raw_base_url) if raw_base_url else None
    if not base_url:
        return jsonify({"error": "A valid Grafana baseUrl is required (https://your-stack.grafana.net)"}), 400

    masked_token = mask_credential_value(api_token)
    logger.info(f"[GRAFANA] Connecting user {user_id} to {base_url} (token={masked_token})")

    client = GrafanaClient(base_url, api_token)

    try:
        org_data = client.get_org()
        user_profile = client.get_user()
    except GrafanaAPIError as exc:
        return jsonify({"error": str(exc)}), 502

    org_name = org_data.get("name") or "Grafana"
    org_id = str(org_data.get("id")) if org_data.get("id") is not None else None
    user_email = user_profile.get("email") if user_profile else None

    token_payload = {
        "api_token": api_token,
        "base_url": base_url,
        "stack_slug": stack_slug,
        "org_name": org_name,
        "org_id": org_id,
        "user_email": user_email,
    }

    try:
        store_tokens_in_db(user_id, token_payload, "grafana")
        logger.info(f"[GRAFANA] Stored credentials for user {user_id} (org={org_name})")
    except Exception as exc:
        logger.exception(f"[GRAFANA] Failed to store credentials for user {user_id}: {exc}")
        return jsonify({"error": "Failed to store Grafana credentials"}), 500

    return jsonify({
        "success": True,
        "org": {
            "name": org_name,
            "id": org_id,
        },
        "baseUrl": base_url,
        "stackSlug": stack_slug,
        "userEmail": user_email,
    })


@grafana_bp.route("/status", methods=["GET", "OPTIONS"])
def status():
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    creds = _get_stored_grafana_credentials(user_id)
    if not creds:
        return jsonify({"connected": False})

    api_token = creds.get("api_token")
    base_url = creds.get("base_url")

    if not api_token or not base_url:
        logger.warning(f"[GRAFANA] Incomplete credentials for user {user_id}")
        return jsonify({"connected": False})

    client = GrafanaClient(base_url, api_token)

    try:
        org_data = client.get_org()
        user_profile = client.get_user()
    except GrafanaAPIError as exc:
        logger.warning(f"[GRAFANA] Status check failed for user {user_id}: {exc}")
        return jsonify({"connected": False, "error": str(exc)})

    return jsonify({
        "connected": True,
        "org": org_data,
        "user": user_profile,
        "baseUrl": base_url,
        "stackSlug": creds.get("stack_slug"),
    })


@grafana_bp.route("/disconnect", methods=["POST", "DELETE", "OPTIONS"])
def disconnect():
    """Disconnect Grafana by removing stored credentials."""
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
                (user_id, "grafana")
            )
            conn.commit()
            deleted_count = cursor.rowcount
        
        logger.info(f"[GRAFANA] Disconnected user {user_id} (deleted {deleted_count} token entries)")
        
        return jsonify({
            "success": True,
            "message": "Grafana disconnected successfully"
        }), 200
        
    except Exception as exc:
        logger.exception(f"[GRAFANA] Failed to disconnect user {user_id}: {exc}")
        return jsonify({"error": "Failed to disconnect Grafana"}), 500


@grafana_bp.route("/alerts/webhook/<user_id>", methods=["POST", "OPTIONS"])
def alert_webhook(user_id: str):
    """Receive alert webhook from Grafana for a specific user."""
    if request.method == "OPTIONS":
        return create_cors_response()

    if not user_id:
        logger.warning("[GRAFANA] Webhook received without user_id")
        return jsonify({"error": "user_id is required"}), 400

    # Check if user has Grafana connected
    creds = get_token_data(user_id, "grafana")
    if not creds:
        logger.warning("[GRAFANA] Webhook received for user %s with no Grafana connection", user_id)
        return jsonify({"error": "Grafana not connected for this user"}), 404

    # Webhook signature verification removed for OSS version


    payload = request.get_json(silent=True) or {}
    logger.info("[GRAFANA] Received alert webhook for user %s: %s", user_id, payload.get("title", "unknown"))

    metadata = {
        "headers": dict(request.headers),
        "remote_addr": request.remote_addr,
    }

    process_grafana_alert.delay(payload, metadata, user_id)

    return jsonify({"received": True})


@grafana_bp.route("/alerts", methods=["GET", "OPTIONS"])
def get_alerts():
    """Fetch Grafana alerts for the authenticated user."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    limit = request.args.get("limit", 50, type=int)
    offset = request.args.get("offset", 0, type=int)
    state_filter = request.args.get("state")  # Optional: filter by alert state

    try:
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()
            
            if state_filter:
                cursor.execute(
                    """
                    SELECT id, alert_uid, alert_title, alert_state, rule_name, 
                           rule_url, dashboard_url, panel_url, payload, received_at, created_at
                    FROM grafana_alerts
                    WHERE user_id = %s AND alert_state = %s
                    ORDER BY received_at DESC
                    LIMIT %s OFFSET %s
                    """,
                    (user_id, state_filter, limit, offset)
                )
            else:
                cursor.execute(
                    """
                    SELECT id, alert_uid, alert_title, alert_state, rule_name, 
                           rule_url, dashboard_url, panel_url, payload, received_at, created_at
                    FROM grafana_alerts
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
                    "SELECT COUNT(*) FROM grafana_alerts WHERE user_id = %s AND alert_state = %s",
                    (user_id, state_filter)
                )
            else:
                cursor.execute(
                    "SELECT COUNT(*) FROM grafana_alerts WHERE user_id = %s",
                    (user_id,)
                )
            total_count = cursor.fetchone()[0]

        return jsonify({
            "alerts": [
                {
                    "id": row[0],
                    "alertUid": row[1],
                    "title": row[2],
                    "state": row[3],
                    "ruleName": row[4],
                    "ruleUrl": row[5],
                    "dashboardUrl": row[6],
                    "panelUrl": row[7],
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
        logger.exception("[GRAFANA] Failed to fetch alerts for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to fetch alerts"}), 500


@grafana_bp.route("/alerts/webhook-url", methods=["GET", "OPTIONS"])
def get_webhook_url():
    """Get the webhook URL that should be configured in Grafana."""
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

    webhook_url = f"{base_url}/grafana/alerts/webhook/{user_id}"

    return jsonify({
        "webhookUrl": webhook_url,
        "instructions": [
            "1. Go to your Grafana instance",
            "2. Navigate to Alerting → Contact points",
            "3. Add a new contact point or edit existing one",
            "4. Select 'Webhook' as the type",
            "5. Paste the webhook URL above",
            "6. (Optional) Add X-Grafana-Signature header for security",
            "7. Save the contact point and add it to your notification policies"
        ]
    })
