"""ServiceNow connector routes: connect, status, disconnect, webhook URL and
secret, the inbound incident webhook, and the per-user RCA settings."""

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from flask import Blueprint, jsonify, request

from routes.servicenow.servicenow_client import (
    ServiceNowAPIError,
    ServiceNowClient,
    is_sys_id,
    normalize_instance_url,
)
from routes.servicenow.tasks import (
    PREF_MIN_PRIORITY,
    PREF_RCA_ENABLED,
    PREF_WORK_NOTES,
    get_rca_settings,
    process_servicenow_event,
)
from routes.servicenow.webhook_auth import (
    WEBHOOK_SECRET_HEADER,
    business_rule_script,
    new_webhook_secret,
    secret_matches,
)
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import store_user_preference
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.secrets.secret_ref_utils import delete_user_secret
from utils.web.public_url import external_backend_url

logger = logging.getLogger(__name__)

servicenow_bp = Blueprint("servicenow", __name__)

PROVIDER = "servicenow"
AUTH_TYPES = ("basic", "oauth_client_credentials")
_SAFE_LOG_RE = re.compile(r"[^a-zA-Z0-9._\-]")
_USER_ID_RE = re.compile(r"^[A-Za-z0-9\-]{1,64}$")
_MAX_FIELD = 512


def _log_safe(value: Any, max_len: int = 80) -> str:
    return _SAFE_LOG_RE.sub("", str(value))[:max_len]


def _stored_credentials(user_id: str) -> Optional[Dict[str, Any]]:
    try:
        creds = get_token_data(user_id, PROVIDER)
        return creds if creds else None
    except Exception:
        logger.exception("[SERVICENOW] Failed to retrieve credentials for user %s", user_id)
        return None


def _status_payload(creds: Dict[str, Any]) -> Dict[str, Any]:
    auth_type = creds.get("auth_type") or "basic"
    return {
        "connected": True,
        "instanceUrl": creds.get("instance_url"),
        "authType": auth_type,
        "displayName": creds.get("display_name") or "ServiceNow",
        "username": creds.get("username") if auth_type == "basic" else None,
        "validatedAt": creds.get("validated_at"),
        "hasWebhookSecret": bool(creds.get("webhook_secret")),
        "capabilities": creds.get("capabilities") or {},
    }


def _webhook_base_url() -> str:
    base_url = external_backend_url()
    if not base_url:
        base_url = request.host_url.rstrip("/")
    return base_url


# ── connection ───────────────────────────────────────────────────────


@servicenow_bp.route("/status", methods=["GET"])
@require_permission("connectors", "read")
def status(user_id):
    """Stored connection details plus a live read of one incident."""
    creds = _stored_credentials(user_id)
    if not creds:
        return jsonify({"connected": False})

    payload = _status_payload(creds)
    try:
        ServiceNowClient.from_credentials(creds).test_connection()
    except (ServiceNowAPIError, ValueError) as exc:
        logger.warning("[SERVICENOW] Status check failed for user %s: %s", user_id, exc)
        payload["connected"] = False
        payload["error"] = str(exc)
    return jsonify(payload)


@servicenow_bp.route("/connect", methods=["POST"])
@require_permission("connectors", "write")
def connect(user_id):
    """Validate the instance and credentials against the Table API, then store them."""
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    try:
        instance_url = normalize_instance_url(str(data.get("instanceUrl") or ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    auth_type = str(data.get("authType") or "basic")
    if auth_type not in AUTH_TYPES:
        return jsonify({"error": "authType must be 'basic' or 'oauth_client_credentials'"}), 400

    def _field(name: str) -> str:
        value = data.get(name)
        return value.strip() if isinstance(value, str) else ""

    username, password = _field("username"), _field("password")
    client_id, client_secret = _field("clientId"), _field("clientSecret")
    display_name = _field("displayName")[:100] or "ServiceNow"

    if auth_type == "basic":
        if not username or not password:
            return jsonify({"error": "username and password are required for basic auth"}), 400
        if len(username) > _MAX_FIELD or len(password) > _MAX_FIELD:
            return jsonify({"error": "username or password is too long"}), 400
    else:
        if not client_id or not client_secret:
            return jsonify({"error": "clientId and clientSecret are required for OAuth client credentials"}), 400
        if len(client_id) > _MAX_FIELD or len(client_secret) > _MAX_FIELD:
            return jsonify({"error": "clientId or clientSecret is too long"}), 400

    client = ServiceNowClient(
        instance_url=instance_url, auth_type=auth_type,
        username=username or None, password=password or None,
        client_id=client_id or None, client_secret=client_secret or None,
    )
    try:
        client.test_connection()
    except ServiceNowAPIError as exc:
        logger.warning("[SERVICENOW] Connection validation failed for user %s: %s", user_id, exc)
        http_status = 400 if exc.status_code in (401, 403) else 502
        return jsonify({"error": str(exc)}), http_status

    existing = _stored_credentials(user_id) or {}
    token_payload: Dict[str, Any] = {
        "instance_url": instance_url,
        "auth_type": auth_type,
        "display_name": display_name,
        "validated_at": datetime.now(timezone.utc).isoformat(),
        # A reconnect keeps the secret so the customer's Business Rule keeps working.
        "webhook_secret": existing.get("webhook_secret") or new_webhook_secret(),
        "capabilities": {"can_read_incidents": True, "can_write_incidents": True},
    }
    if auth_type == "basic":
        token_payload["username"] = username
        token_payload["password"] = password
    else:
        token_payload["client_id"] = client_id
        token_payload["client_secret"] = client_secret

    try:
        store_tokens_in_db(user_id, token_payload, PROVIDER)
    except Exception:
        logger.exception("[SERVICENOW] Failed to store credentials for user %s", user_id)
        return jsonify({"error": "Failed to store credentials"}), 500

    logger.info("[SERVICENOW] Connected user %s to %s", user_id, _log_safe(instance_url))
    return jsonify({"success": True, **_status_payload(token_payload)})


@servicenow_bp.route("/disconnect", methods=["POST", "DELETE"])
@require_permission("connectors", "write")
def disconnect(user_id):
    try:
        success, _ = delete_user_secret(user_id, PROVIDER)
        if not success:
            return jsonify({"error": "Failed to delete stored credentials"}), 500
        logger.info("[SERVICENOW] Disconnected user %s", user_id)
        return jsonify({"success": True, "message": "ServiceNow disconnected successfully"})
    except Exception:
        logger.exception("[SERVICENOW] Failed to disconnect user %s", user_id)
        return jsonify({"error": "Failed to disconnect ServiceNow"}), 500


# ── webhook configuration ────────────────────────────────────────────


@servicenow_bp.route("/webhook-url", methods=["GET"])
@require_permission("connectors", "read")
def get_webhook_url(user_id):
    """Webhook URL, shared secret and the Business Rule script for this user."""
    creds = _stored_credentials(user_id)
    if not creds:
        return jsonify({"error": "ServiceNow is not connected"}), 400

    if not creds.get("webhook_secret"):
        creds["webhook_secret"] = new_webhook_secret()
        try:
            store_tokens_in_db(user_id, creds, PROVIDER)
        except Exception:
            logger.exception("[SERVICENOW] Failed to store webhook secret for user %s", user_id)
            return jsonify({"error": "Failed to store webhook secret"}), 500

    webhook_url = f"{_webhook_base_url()}/servicenow/webhook/{user_id}"
    return jsonify({
        "webhookUrl": webhook_url,
        "webhookSecret": creds["webhook_secret"],
        "headerName": WEBHOOK_SECRET_HEADER,
        "instructions": [
            "1. In ServiceNow, open System Properties and create two properties: "
            "x_aurora.webhook_url (the URL above) and x_aurora.webhook_secret (the secret above, type password).",
            "2. Open System Definition > Business Rules and click New.",
            "3. Table: Incident [incident]. Tick Advanced. When: After. Tick Insert (tick Update too if you "
            "want resolved and closed states mirrored). Add filter conditions if only some incidents should be investigated.",
            "4. Paste the script below into the Script tab and save.",
            "5. Create a test incident. It appears on the Aurora Incidents page within seconds.",
        ],
        "businessRuleScript": business_rule_script(),
    })


@servicenow_bp.route("/webhook-secret/rotate", methods=["POST"])
@require_permission("connectors", "write")
def rotate_webhook_secret(user_id):
    """Issue a new shared secret; the old one stops working immediately."""
    creds = _stored_credentials(user_id)
    if not creds:
        return jsonify({"error": "ServiceNow is not connected"}), 400
    creds["webhook_secret"] = new_webhook_secret()
    try:
        store_tokens_in_db(user_id, creds, PROVIDER)
    except Exception:
        logger.exception("[SERVICENOW] Failed to rotate webhook secret for user %s", user_id)
        return jsonify({"error": "Failed to rotate webhook secret"}), 500
    logger.info("[SERVICENOW] Webhook secret rotated for user %s", user_id)
    return jsonify({"success": True, "webhookSecret": creds["webhook_secret"]})


# ── inbound webhook (no session; authenticated by the shared secret) ──


@servicenow_bp.route("/webhook/<user_id>", methods=["POST"])
def webhook(user_id: str):
    """Receive an incident record from the customer's Business Rule."""
    if not user_id or not _USER_ID_RE.fullmatch(user_id):
        return jsonify({"error": "user_id is required"}), 400

    log_uid = _log_safe(user_id, 36)
    creds = get_token_data(user_id, PROVIDER)
    if not creds:
        logger.warning("[SERVICENOW] Webhook with no connection: %s", log_uid)
        return jsonify({"error": "ServiceNow not connected for this user"}), 404

    if not secret_matches(
        creds.get("webhook_secret"),
        request.headers.get(WEBHOOK_SECRET_HEADER),
        request.headers.get("Authorization"),
    ):
        logger.warning("[SERVICENOW] Webhook rejected: bad or missing secret: %s", log_uid)
        return jsonify({"error": "Invalid webhook secret"}), 401

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Invalid or missing JSON payload"}), 400
    sys_id = payload.get("sys_id")
    if not is_sys_id(sys_id):
        return jsonify({"error": "sys_id is required"}), 400

    logger.info(
        "[SERVICENOW] Webhook received: user=%s number=%s event=%s",
        log_uid, _log_safe(payload.get("number", "")), _log_safe(payload.get("event", "inserted")),
    )
    try:
        process_servicenow_event.delay(payload, user_id)
    except Exception:
        # Broker failure is on our side: 503 so the Business Rule logs it as a retryable error.
        logger.exception("[SERVICENOW] Failed to enqueue webhook for user %s", log_uid)
        return jsonify({"error": "could not enqueue webhook for processing"}), 503

    return jsonify({"received": True})


# ── RCA settings ─────────────────────────────────────────────────────


@servicenow_bp.route("/rca-settings", methods=["GET"])
@require_permission("connectors", "read")
def get_rca_settings_route(user_id):
    return jsonify(get_rca_settings(user_id))


@servicenow_bp.route("/rca-settings", methods=["PUT"])
@require_permission("connectors", "write")
def update_rca_settings(user_id):
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    rca_enabled = data.get("rcaEnabled")
    min_priority = data.get("minPriority")
    work_notes = data.get("workNotesEnabled")

    if rca_enabled is not None:
        if not isinstance(rca_enabled, bool):
            return jsonify({"error": "rcaEnabled must be a boolean"}), 400
        store_user_preference(user_id, PREF_RCA_ENABLED, rca_enabled)

    if min_priority is not None:
        if isinstance(min_priority, bool) or not isinstance(min_priority, int) or not 1 <= min_priority <= 5:
            return jsonify({"error": "minPriority must be an integer from 1 to 5"}), 400
        store_user_preference(user_id, PREF_MIN_PRIORITY, min_priority)

    if work_notes is not None:
        if not isinstance(work_notes, bool):
            return jsonify({"error": "workNotesEnabled must be a boolean"}), 400
        if work_notes and not _stored_credentials(user_id):
            return jsonify({"error": "Connect ServiceNow before enabling work notes"}), 400
        store_user_preference(user_id, PREF_WORK_NOTES, work_notes)

    return jsonify(get_rca_settings(user_id))
