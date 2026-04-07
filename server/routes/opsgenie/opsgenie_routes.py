import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import requests
from flask import Blueprint, jsonify, request

from routes.opsgenie.config import OPSGENIE_TIMEOUT, REGION_URLS
from routes.opsgenie.tasks import process_opsgenie_event
from utils.db.connection_pool import db_pool
from utils.web.cors_utils import create_cors_response
from utils.logging.secure_logging import mask_credential_value
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request
from utils.secrets.secret_ref_utils import delete_user_secret

logger = logging.getLogger(__name__)

opsgenie_bp = Blueprint("opsgenie", __name__)


class OpsGenieAPIError(Exception):
    """Custom error for OpsGenie API interactions."""

    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


class OpsGenieClient:
    def __init__(self, api_key: str, region: str = "us"):
        self.api_key = api_key
        self.region = region
        self.base_url = REGION_URLS.get(region, REGION_URLS["us"])

    @property
    def headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"GenieKey {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.base_url}{path}"
        try:
            response = requests.request(
                method, url, headers=self.headers, timeout=OPSGENIE_TIMEOUT, **kwargs
            )
        except requests.RequestException as exc:
            logger.error("[OPSGENIE] %s %s network error: %s", method, url, exc, exc_info=True)
            raise OpsGenieAPIError("Unable to reach OpsGenie") from exc

        # OpsGenie uses X-RateLimit-State header for rate limiting
        if response.headers.get("X-RateLimit-State") == "THROTTLED" or response.status_code == 429:
            logger.warning("[OPSGENIE] Rate limited on %s %s", method, path)
            raise OpsGenieAPIError("OpsGenie API rate limit reached. Please retry later.", status_code=429)

        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            logger.error(
                "[OPSGENIE] %s %s failed (%s): %s",
                method, url, response.status_code, response.text,
            )
            raise OpsGenieAPIError(response.text or str(exc), status_code=response.status_code) from exc

        return response

    # ── Account ───────────────────────────────────────────────────────
    def validate_connection(self) -> Dict[str, Any]:
        """GET /v2/account — returns account name, plan info."""
        return self._request("GET", "/v2/account").json()

    # ── Alerts ────────────────────────────────────────────────────────
    def list_alerts(
        self,
        query: Optional[str] = None,
        offset: int = 0,
        limit: int = 50,
        sort: str = "createdAt",
        order: str = "desc",
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "offset": offset,
            "limit": max(1, min(limit, 100)),
            "sort": sort,
            "order": order,
        }
        if query:
            params["query"] = query
        return self._request("GET", "/v2/alerts", params=params).json()

    def get_alert(self, alert_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/v2/alerts/{alert_id}").json()

    def get_alert_logs(self, alert_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/v2/alerts/{alert_id}/logs").json()

    def get_alert_notes(self, alert_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/v2/alerts/{alert_id}/notes").json()

    # ── Incidents ─────────────────────────────────────────────────────
    def list_incidents(
        self,
        query: Optional[str] = None,
        offset: int = 0,
        limit: int = 50,
        sort: str = "createdAt",
        order: str = "desc",
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "offset": offset,
            "limit": max(1, min(limit, 100)),
            "sort": sort,
            "order": order,
        }
        if query:
            params["query"] = query
        return self._request("GET", "/v1/incidents", params=params).json()

    def get_incident(self, incident_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/v1/incidents/{incident_id}").json()

    def get_incident_timeline(self, incident_id: str) -> Dict[str, Any]:
        return self._request(
            "GET", f"/v2/incident-timelines/{incident_id}/entries"
        ).json()

    # ── Services ──────────────────────────────────────────────────────
    def list_services(self, offset: int = 0, limit: int = 50) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "offset": offset,
            "limit": max(1, min(limit, 100)),
        }
        return self._request("GET", "/v1/services", params=params).json()

    # ── Schedules / On-Calls ──────────────────────────────────────────
    def get_on_calls(self, schedule_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/v2/schedules/{schedule_id}/on-calls").json()

    def list_schedules(self) -> Dict[str, Any]:
        return self._request("GET", "/v2/schedules").json()

    # ── Teams ─────────────────────────────────────────────────────────
    def list_teams(self) -> Dict[str, Any]:
        return self._request("GET", "/v2/teams").json()


# ── Credential helpers ────────────────────────────────────────────────


def _get_stored_opsgenie_credentials(user_id: str) -> Optional[Dict[str, Any]]:
    try:
        data = get_token_data(user_id, "opsgenie")
        if data:
            return data

        org_id = get_org_id_from_request()
        if not org_id:
            return None

        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT user_id FROM user_tokens WHERE org_id = %s AND provider = 'opsgenie' AND is_active = TRUE AND secret_ref IS NOT NULL LIMIT 1",
                (org_id,)
            )
            row = cursor.fetchone()

        if row:
            data = get_token_data(row[0], "opsgenie")
            return data

        return None
    except Exception as exc:
        logger.error("[OPSGENIE] Failed to retrieve credentials for user %s: %s", user_id, exc)
        return None


def _build_client_from_creds(creds: Dict[str, Any]) -> Optional[OpsGenieClient]:
    api_key = creds.get("api_key")
    region = creds.get("region", "us")
    if not api_key:
        return None
    # JSM-ready: later can check creds.get("auth_type") to return a JSMOperationsClient instead
    return OpsGenieClient(api_key=api_key, region=region)


# ── Routes ────────────────────────────────────────────────────────────


@opsgenie_bp.route("/connect", methods=["POST", "OPTIONS"])
@require_permission("connectors", "write")
def connect(user_id):
    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        logger.debug("Failed to parse JSON payload for OpsGenie connect")
        payload = {}

    api_key = payload.get("apiKey")
    region = payload.get("region", "us")

    if not api_key or not isinstance(api_key, str):
        return jsonify({"error": "OpsGenie API key is required"}), 400

    if region not in REGION_URLS:
        return jsonify({"error": f"Invalid region '{region}'. Must be one of: {', '.join(REGION_URLS)}"}), 400

    masked_key = mask_credential_value(api_key)
    logger.info("[OPSGENIE] Connecting user %s to region=%s key=%s", user_id, region, masked_key)

    client = OpsGenieClient(api_key=api_key, region=region)

    try:
        account_info = client.validate_connection()
        account_data = account_info.get("data", {})
    except OpsGenieAPIError as exc:
        logger.error("[OPSGENIE] Credential validation failed for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to validate OpsGenie credentials"}), 502

    token_payload = {
        "api_key": api_key,
        "region": region,
        "account_name": account_data.get("name"),
        "plan": account_data.get("plan", {}).get("name") if isinstance(account_data.get("plan"), dict) else None,
        "validated_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        store_tokens_in_db(user_id, token_payload, "opsgenie")
        logger.info("[OPSGENIE] Stored credentials for user %s (region=%s)", user_id, region)
    except Exception as exc:
        logger.exception("[OPSGENIE] Failed to store credentials: %s", exc)
        return jsonify({"error": "Failed to store OpsGenie credentials"}), 500

    response = {
        "success": True,
        "region": region,
        "accountName": account_data.get("name"),
        "plan": account_data.get("plan"),
        "validated": True,
    }
    return jsonify(response)


@opsgenie_bp.route("/status", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def status(user_id):
    creds = _get_stored_opsgenie_credentials(user_id)
    if not creds:
        return jsonify({"connected": False})

    client = _build_client_from_creds(creds)
    if not client:
        logger.warning("[OPSGENIE] Incomplete credentials for user %s", user_id)
        return jsonify({"connected": False})

    try:
        account_info = client.validate_connection()
        account_data = account_info.get("data", {})
    except OpsGenieAPIError as exc:
        logger.warning("[OPSGENIE] Status validation failed for user %s: %s", user_id, exc)
        return jsonify({"connected": False, "error": "Failed to validate stored OpsGenie credentials"})

    return jsonify({
        "connected": True,
        "region": creds.get("region"),
        "accountName": account_data.get("name"),
        "plan": account_data.get("plan"),
    })


@opsgenie_bp.route("/disconnect", methods=["DELETE", "POST", "OPTIONS"])
@require_permission("connectors", "write")
def disconnect(user_id):
    try:
        success, token_rows = delete_user_secret(user_id, "opsgenie")
        if not success:
            logger.warning("[OPSGENIE] Failed to clean up secrets during disconnect")

        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM opsgenie_events WHERE user_id = %s",
                (user_id,)
            )
            event_rows = cursor.rowcount
            conn.commit()

        logger.info("[OPSGENIE] Disconnected provider (tokens=%s, events=%s)", token_rows, event_rows)
        return jsonify({
            "success": True,
            "message": "OpsGenie disconnected successfully",
            "tokensDeleted": token_rows,
            "eventsDeleted": event_rows,
        })
    except Exception as exc:
        logger.exception("[OPSGENIE] Failed to disconnect provider")
        return jsonify({"error": "Failed to disconnect OpsGenie"}), 500


@opsgenie_bp.route("/webhook/<user_id>", methods=["POST", "OPTIONS"])
def webhook(user_id: str):
    if request.method == "OPTIONS":
        return create_cors_response()

    # Check if user has OpsGenie connected
    creds = get_token_data(user_id, "opsgenie")
    if not creds:
        logger.warning("[OPSGENIE] Webhook received for user %s with no OpsGenie connection", user_id)
        return jsonify({"error": "OpsGenie not connected for this user"}), 404

    payload = request.get_json(silent=True) or {}
    metadata = {
        "headers": dict(request.headers),
        "remote_addr": request.remote_addr,
    }
    logger.info("[OPSGENIE] Received webhook for user %s action=%s", user_id, payload.get("action"))

    process_opsgenie_event.delay(payload=payload, metadata=metadata, user_id=user_id)
    return jsonify({"received": True})


@opsgenie_bp.route("/webhook-url", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def webhook_url(user_id):
    # Use ngrok URL for development if available, otherwise use backend URL
    ngrok_url = os.getenv("NGROK_URL", "").rstrip("/")
    backend_url = os.getenv("NEXT_PUBLIC_BACKEND_URL", "").rstrip("/")

    # For development, prefer ngrok URL if available
    if ngrok_url and backend_url.startswith("http://localhost"):
        base_url = ngrok_url
    else:
        base_url = backend_url

    url = f"{base_url}/opsgenie/webhook/{user_id}"

    instructions = [
        "1. Navigate to Settings → Integrations in OpsGenie.",
        "2. Add a new Webhook (Outgoing) integration.",
        "3. Paste the URL above into the webhook URL field.",
        "4. Select the alert actions you want to receive (e.g. Create, Acknowledge, Close).",
        "5. Save the integration and test the webhook to verify connectivity.",
    ]

    return jsonify({
        "webhookUrl": url,
        "instructions": instructions,
    })


@opsgenie_bp.route("/events/ingested", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def list_ingested_events(user_id):
    org_id = get_org_id_from_request()
    limit = request.args.get("limit", default=50, type=int)
    offset = request.args.get("offset", default=0, type=int)
    status_filter = request.args.get("status")
    type_filter = request.args.get("event_type")

    try:
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SET myapp.current_org_id = %s", (org_id,))

            base_query = """
                SELECT id, action, alert_message, status, source, payload, received_at, created_at
                FROM opsgenie_events
                WHERE org_id = %s
            """
            params = [org_id]
            if status_filter:
                base_query += " AND status = %s"
                params.append(status_filter)
            if type_filter:
                base_query += " AND action = %s"
                params.append(type_filter)

            base_query += " ORDER BY received_at DESC LIMIT %s OFFSET %s"
            params.extend([limit, offset])

            cursor.execute(base_query, params)
            rows = cursor.fetchall()

            count_query = "SELECT COUNT(*) FROM opsgenie_events WHERE org_id = %s"
            count_params = [org_id]
            if status_filter:
                count_query += " AND status = %s"
                count_params.append(status_filter)
            if type_filter:
                count_query += " AND action = %s"
                count_params.append(type_filter)

            cursor.execute(count_query, count_params)
            total = cursor.fetchone()[0]

        events = []
        for row in rows:
            events.append({
                "id": row[0],
                "action": row[1],
                "alertMessage": row[2],
                "status": row[3],
                "source": row[4],
                "payload": row[5],
                "receivedAt": row[6].isoformat() if row[6] else None,
                "createdAt": row[7].isoformat() if row[7] else None,
            })

        return jsonify({
            "events": events,
            "total": total,
            "limit": limit,
            "offset": offset,
        })
    except Exception as exc:
        logger.exception("[OPSGENIE] Failed to list ingested events for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to load OpsGenie webhook events"}), 500
