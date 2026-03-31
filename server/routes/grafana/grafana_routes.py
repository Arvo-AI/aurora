import ipaddress
import logging
import os
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import requests
from flask import Blueprint, jsonify, request

from routes.grafana.tasks import process_grafana_alert
from utils.db.connection_pool import db_pool
from utils.web.cors_utils import create_cors_response
from utils.logging.secure_logging import mask_credential_value
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request
from utils.secrets.secret_ref_utils import delete_user_secret
GRAFANA_TIMEOUT = 15

# Short hint for UI toasts; full guidance lives in website/docs and GrafanaConnectionStep.
_GRAFANA_SELF_HOSTED_URL_HINT = "See Grafana integration docs for self-hosted URL and Docker."

logger = logging.getLogger(__name__)

grafana_bp = Blueprint("grafana", __name__)


class GrafanaAPIError(Exception):
    """Custom error for Grafana API interactions."""


def _host_allows_insecure_http(hostname: str) -> bool:
    """Allow http:// only for local/self-hosted targets (not public internet hosts)."""
    h = hostname.strip().lower().rstrip(".")
    if h in ("localhost", "host.docker.internal"):
        return True
    if h.endswith(".local"):
        return True
    # Docker / k8s-style single-label names (e.g. grafana, metrics-server) on internal DNS.
    if "." not in h and 1 <= len(h) <= 63 and all(c.isalnum() or c in "-_" for c in h):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return bool(ip.is_loopback or ip.is_private or ip.is_link_local)


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

        scheme_sep = url.find("://")
        if scheme_sep != -1:
            explicit_scheme = url[:scheme_sep].lower()
            if explicit_scheme not in ("http", "https"):
                return None

        lower = url.lower()
        if not (lower.startswith("http://") or lower.startswith("https://")):
            url = "https://" + url

        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return None
        if not parsed.netloc or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        try:
            port = parsed.port
        except ValueError:
            return None
        if port is not None and not (1 <= port <= 65535):
            return None
        if parsed.scheme == "http" and not _host_allows_insecure_http(parsed.hostname):
            return None

        return url.rstrip("/")

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
@require_permission("connectors", "write")
def connect(user_id):
    """Store Grafana API token and validate connectivity."""
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    api_token = (data.get("apiToken") or data.get("token") or "").strip()
    raw_base_url = data.get("baseUrl")
    stack_slug = data.get("stackSlug")

    if not api_token or not isinstance(api_token, str):
        return jsonify({"error": "Grafana API token is required"}), 400

    base_url = GrafanaClient.normalize_base_url(raw_base_url) if raw_base_url else None
    if not base_url:
        return jsonify(
            {
                "error": (
                    "A valid Grafana baseUrl is required (e.g. https://your-stack.grafana.net). "
                    + _GRAFANA_SELF_HOSTED_URL_HINT
                ),
            }
        ), 400

    masked_token = mask_credential_value(api_token)
    logger.info(f"[GRAFANA] Connecting user {user_id} to {base_url} (token={masked_token})")

    client = GrafanaClient(base_url, api_token)

    try:
        org_data = client.get_org()
        user_profile = client.get_user()
    except GrafanaAPIError as exc:
        logger.error(f"[GRAFANA] Connection validation failed for user {user_id}: {exc}")
        err_text = str(exc)
        if err_text == "Unable to reach Grafana":
            return jsonify(
                {
                    "error": (
                        "Unable to reach Grafana from the Aurora server. "
                        + _GRAFANA_SELF_HOSTED_URL_HINT
                    ),
                }
            ), 502
        return jsonify({"error": "Failed to validate Grafana credentials. Ensure the service account has the Admin role."}), 502

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
@require_permission("connectors", "read")
def status(user_id):
    creds = _get_stored_grafana_credentials(user_id)
    if not creds:
        return jsonify({"connected": False})

    api_token = creds.get("api_token")
    base_url = creds.get("base_url")

    if not api_token or not base_url:
        logger.warning(f"[GRAFANA] Incomplete credentials for user {user_id}")
        return jsonify({"connected": False})

    return jsonify({
        "connected": True,
        "org": {"name": creds.get("org_name"), "id": creds.get("org_id")},
        "user": {"email": creds.get("user_email")} if creds.get("user_email") else None,
        "baseUrl": base_url,
        "stackSlug": creds.get("stack_slug"),
    })


@grafana_bp.route("/disconnect", methods=["POST", "DELETE", "OPTIONS"])
@require_permission("connectors", "write")
def disconnect(user_id):
    """Disconnect Grafana by removing stored credentials."""
    try:
        success, deleted_count = delete_user_secret(user_id, "grafana")
        if not success:
            logger.warning("[GRAFANA] Failed to clean up secrets during disconnect")
            return jsonify({"success": False, "error": "Failed to delete stored credentials"}), 500

        logger.info("[GRAFANA] Disconnected provider (deleted %s token entries)", deleted_count)

        return jsonify({
            "success": True,
            "message": "Grafana disconnected successfully",
            "deleted": deleted_count
        }), 200

    except Exception as exc:
        logger.exception("[GRAFANA] Failed to disconnect provider")
        return jsonify({"error": "Failed to disconnect Grafana"}), 500


@grafana_bp.route("/alerts/webhook", methods=["POST", "OPTIONS"], strict_slashes=False)
def alert_webhook_missing_user_id():
    """Grafana contact points must use the full URL including your Aurora user id."""
    if request.method == "OPTIONS":
        return create_cors_response()
    return jsonify(
        {
            "error": (
                "Missing user id in the webhook path. Copy the full URL from "
                "Aurora → Grafana integration (it ends with /grafana/alerts/webhook/<user_id>)."
            )
        }
    ), 400


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
@require_permission("connectors", "read")
def get_alerts(user_id):
    """Fetch Grafana alerts for the authenticated user."""
    org_id = get_org_id_from_request()
    limit = request.args.get("limit", 50, type=int)
    offset = request.args.get("offset", 0, type=int)
    state_filter = request.args.get("state")  # Optional: filter by alert state

    try:
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SET myapp.current_org_id = %s", (org_id,))
            
            if state_filter:
                cursor.execute(
                    """
                    SELECT id, alert_uid, alert_title, alert_state, rule_name, 
                           rule_url, dashboard_url, panel_url, payload, received_at, created_at
                    FROM grafana_alerts
                    WHERE org_id = %s AND alert_state = %s
                    ORDER BY received_at DESC
                    LIMIT %s OFFSET %s
                    """,
                    (org_id, state_filter, limit, offset)
                )
            else:
                cursor.execute(
                    """
                    SELECT id, alert_uid, alert_title, alert_state, rule_name, 
                           rule_url, dashboard_url, panel_url, payload, received_at, created_at
                    FROM grafana_alerts
                    WHERE org_id = %s
                    ORDER BY received_at DESC
                    LIMIT %s OFFSET %s
                    """,
                    (org_id, limit, offset)
                )
            
            alerts = cursor.fetchall()
            
            # Get total count
            if state_filter:
                cursor.execute(
                    "SELECT COUNT(*) FROM grafana_alerts WHERE org_id = %s AND alert_state = %s",
                    (org_id, state_filter)
                )
            else:
                cursor.execute(
                    "SELECT COUNT(*) FROM grafana_alerts WHERE org_id = %s",
                    (org_id,)
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
@require_permission("connectors", "read")
def get_webhook_url(user_id):
    """Get the webhook URL that should be configured in Grafana."""
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
