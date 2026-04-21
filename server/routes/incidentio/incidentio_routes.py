"""incident.io connector routes: connect, status, disconnect, webhook, alerts, settings."""

import hashlib
import hmac
import logging
import os
import secrets
from typing import Any, Dict, Optional

import requests
from flask import Blueprint, jsonify, request

from routes.incidentio.tasks import process_incidentio_event
from utils.db.connection_pool import db_pool
from utils.web.cors_utils import create_cors_response
from utils.auth.stateless_auth import (
    get_org_id_from_request,
    get_user_preference,
    store_user_preference,
)
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.auth.rbac_decorators import require_permission
from utils.secrets.secret_ref_utils import delete_user_secret

INCIDENTIO_API_BASE = "https://api.incident.io/v2"
INCIDENTIO_TIMEOUT = 15

logger = logging.getLogger(__name__)

incidentio_bp = Blueprint("incidentio", __name__)


class IncidentioAPIError(Exception):
    pass


class IncidentioClient:
    """Client for the incident.io REST API."""

    def __init__(self, api_key: str):
        self.api_key = api_key

    @property
    def headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{INCIDENTIO_API_BASE}{path}"
        try:
            response = requests.request(
                method, url, headers=self.headers, timeout=INCIDENTIO_TIMEOUT, **kwargs
            )
            response.raise_for_status()
            return response
        except requests.exceptions.Timeout as exc:
            raise IncidentioAPIError("Connection to incident.io timed out") from exc
        except requests.exceptions.ConnectionError as exc:
            raise IncidentioAPIError("Unable to reach incident.io API") from exc
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            body = ""
            try:
                body = exc.response.text if exc.response is not None else ""
            except Exception:
                body = "(unreadable response body)"
            logger.warning("[INCIDENTIO] HTTP %s from %s: %s", status, path, body[:500])
            if status == 401:
                raise IncidentioAPIError("Invalid API key") from exc
            if status == 403:
                raise IncidentioAPIError(f"API key lacks required permissions: {body[:200]}") from exc
            raise IncidentioAPIError(f"incident.io API error ({status}): {body[:200]}") from exc

    def list_incidents(self, page_size: int = 5) -> Dict[str, Any]:
        return self._request("GET", "/incidents", params={"page_size": page_size}).json()

    def get_incident(self, incident_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/incidents/{incident_id}").json()

    def get_incident_updates(self, incident_id: str) -> Dict[str, Any]:
        return self._request("GET", "/incident_updates", params={"incident_id": incident_id}).json()

    def post_incident_update(self, incident_id: str, message: str) -> Dict[str, Any]:
        return self._request(
            "POST",
            "/incident_updates",
            json={"incident_id": incident_id, "message": message},
        ).json()


def _get_stored_credentials(user_id: str) -> Optional[Dict[str, Any]]:
    try:
        creds = get_token_data(user_id, "incidentio")
        return creds if creds else None
    except Exception as exc:
        logger.error("[INCIDENTIO] Failed to retrieve credentials for user %s: %s", user_id, exc)
        return None


# ── Routes ──────────────────────────────────────────────────────────


@incidentio_bp.route("/connect", methods=["POST", "OPTIONS"])
@require_permission("connectors", "write")
def connect(user_id):
    """Validate API key against incident.io and store credentials."""
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    api_key = data.get("apiKey")
    if not api_key or not isinstance(api_key, str):
        return jsonify({"error": "apiKey is required"}), 400

    if len(api_key) < 20 or len(api_key) > 500:
        return jsonify({"error": "Invalid API key format"}), 400

    client = IncidentioClient(api_key)
    try:
        client.list_incidents(page_size=1)
    except IncidentioAPIError as exc:
        logger.warning("[INCIDENTIO] Connection validation failed for user %s: %s", user_id, exc)
        msg = str(exc)
        safe_messages = {"Invalid API key", "API key lacks required permissions", "Connection to incident.io timed out", "Unable to reach incident.io API"}
        if not any(msg.startswith(s) for s in safe_messages):
            msg = "Failed to validate API key with incident.io"
        return jsonify({"error": msg}), 502

    token_payload = {"api_key": api_key, "webhook_secret": secrets.token_hex(32)}

    try:
        store_tokens_in_db(user_id, token_payload, "incidentio")
    except Exception as exc:
        logger.exception("[INCIDENTIO] Failed to store credentials for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to store credentials"}), 500

    return jsonify({"success": True, "connected": True})


@incidentio_bp.route("/status", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def status(user_id):
    """Check incident.io connection status."""
    creds = _get_stored_credentials(user_id)
    if not creds:
        return jsonify({"connected": False})

    api_key = creds.get("api_key")
    if not api_key:
        return jsonify({"connected": False})

    client = IncidentioClient(api_key)
    try:
        client.list_incidents(page_size=1)
    except IncidentioAPIError as exc:
        logger.warning("[INCIDENTIO] Status check failed for user %s: %s", user_id, exc)
        return jsonify({"connected": False, "error": "Connection check failed"})

    return jsonify({"connected": True})


@incidentio_bp.route("/disconnect", methods=["POST", "DELETE", "OPTIONS"])
@require_permission("connectors", "write")
def disconnect(user_id):
    """Remove stored incident.io credentials."""
    try:
        success, deleted_count = delete_user_secret(user_id, "incidentio")
        if not success:
            return jsonify({"error": "Failed to delete stored credentials"}), 500

        logger.info("[INCIDENTIO] Disconnected user %s", user_id)
        return jsonify({"success": True, "message": "incident.io disconnected successfully"})
    except Exception:
        logger.exception("[INCIDENTIO] Failed to disconnect user %s", user_id)
        return jsonify({"error": "Failed to disconnect incident.io"}), 500


@incidentio_bp.route("/alerts/webhook/<user_id>", methods=["POST", "OPTIONS"])
def alert_webhook(user_id: str):
    """Receive webhook events from incident.io."""
    if request.method == "OPTIONS":
        return create_cors_response()

    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    creds = get_token_data(user_id, "incidentio")
    if not creds:
        logger.warning("[INCIDENTIO] Webhook for user %s with no connection", user_id)
        return jsonify({"error": "incident.io not connected for this user"}), 404

    webhook_secret = creds.get("webhook_secret")
    signature = request.headers.get("X-Aurora-Signature", "")
    if webhook_secret:
        if not signature:
            logger.warning("[INCIDENTIO] Webhook rejected: missing X-Aurora-Signature for user %s", user_id[:50])
            return jsonify({"error": "Missing X-Aurora-Signature header"}), 401
        expected = hmac.new(webhook_secret.encode(), request.get_data(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            logger.warning("[INCIDENTIO] Webhook rejected: invalid signature for user %s", user_id[:50])
            return jsonify({"error": "Invalid webhook signature"}), 401

    payload = request.get_json(silent=True) or {}

    event_type = payload.get("event_type") or (payload.get("event", {}) or {}).get("type", "unknown")
    logger.info("[INCIDENTIO] Webhook for user %s: event_type=%s", user_id, event_type)

    metadata = {"remote_addr": request.remote_addr}
    process_incidentio_event.delay(payload, metadata, user_id)

    return jsonify({"received": True})


@incidentio_bp.route("/alerts", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def get_alerts(user_id):
    """Fetch stored incident.io events."""
    org_id = get_org_id_from_request()
    limit = min(max(request.args.get("limit", 50, type=int), 1), 200)
    offset = max(request.args.get("offset", 0, type=int), 0)
    severity_filter = request.args.get("severity")

    try:
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SET myapp.current_org_id = %s", (org_id,))

            where = "WHERE org_id = %s"
            params = [org_id]
            if severity_filter:
                where += " AND severity = %s"
                params.append(severity_filter)

            cursor.execute(
                f"""
                SELECT id, incident_id, incident_name, incident_status, severity,
                       incident_type, payload, received_at, created_at
                FROM incidentio_alerts
                {where}
                ORDER BY received_at DESC
                LIMIT %s OFFSET %s
                """,
                (*params, limit, offset),
            )
            rows = cursor.fetchall()

            cursor.execute(f"SELECT COUNT(*) FROM incidentio_alerts {where}", params)
            total = cursor.fetchone()[0]

        return jsonify({
            "alerts": [
                {
                    "id": r[0],
                    "incidentId": r[1],
                    "name": r[2],
                    "status": r[3],
                    "severity": r[4],
                    "incidentType": r[5],
                    "payload": r[6],
                    "receivedAt": r[7].isoformat() if r[7] else None,
                    "createdAt": r[8].isoformat() if r[8] else None,
                }
                for r in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        })
    except Exception as exc:
        logger.exception("[INCIDENTIO] Failed to fetch alerts: %s", exc)
        return jsonify({"error": "Failed to fetch alerts"}), 500


@incidentio_bp.route("/alerts/webhook-url", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def get_webhook_url(user_id):
    """Return the webhook URL for this user's incident.io configuration."""
    ngrok_url = os.getenv("NGROK_URL", "").rstrip("/")
    frontend_url = os.getenv("FRONTEND_URL", "").rstrip("/")
    backend_url = os.getenv("NEXT_PUBLIC_BACKEND_URL", "").rstrip("/")

    if ngrok_url:
        webhook_url = f"{ngrok_url}/api/incident-io/alerts/webhook/{user_id}"
    elif frontend_url and not frontend_url.startswith("http://localhost"):
        webhook_url = f"{frontend_url}/api/incident-io/alerts/webhook/{user_id}"
    else:
        webhook_url = f"{backend_url}/incidentio/alerts/webhook/{user_id}"

    return jsonify({
        "webhookUrl": webhook_url,
        "instructions": [
            "1. Go to incident.io → Settings → Webhooks",
            "2. Click 'Add endpoint'",
            "3. Paste the webhook URL above",
            "4. Select events: incident.created, incident.updated",
            "5. Save — incidents will now trigger Aurora RCA automatically",
        ],
    })


@incidentio_bp.route("/rca-settings", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def get_rca_settings(user_id):
    rca_enabled = get_user_preference(user_id, "incidentio_rca_enabled", default=False)
    postback_enabled = get_user_preference(user_id, "incidentio_postback_enabled", default=False)
    return jsonify({"rcaEnabled": rca_enabled, "postbackEnabled": postback_enabled})


@incidentio_bp.route("/rca-settings", methods=["PUT", "OPTIONS"])
@require_permission("connectors", "write")
def update_rca_settings(user_id):
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    rca_enabled = data.get("rcaEnabled")
    postback_enabled = data.get("postbackEnabled")

    if rca_enabled is not None:
        if not isinstance(rca_enabled, bool):
            return jsonify({"error": "rcaEnabled must be a boolean"}), 400
        store_user_preference(user_id, "incidentio_rca_enabled", rca_enabled)

    if postback_enabled is not None:
        if not isinstance(postback_enabled, bool):
            return jsonify({"error": "postbackEnabled must be a boolean"}), 400
        store_user_preference(user_id, "incidentio_postback_enabled", postback_enabled)

    return jsonify({
        "success": True,
        "rcaEnabled": get_user_preference(user_id, "incidentio_rca_enabled", default=False),
        "postbackEnabled": get_user_preference(user_id, "incidentio_postback_enabled", default=False),
    })
