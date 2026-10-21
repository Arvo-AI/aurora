"""ServiceNow connector routes: connect, status, disconnect, webhook URL and
secret, the inbound incident webhook, the org RCA settings and the option
catalog behind the filter pickers."""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from flask import Blueprint, jsonify, request

from routes.servicenow.field_options import build_field_options
from routes.servicenow.rca_filters import decide_webhook, normalize_rca_rules
from routes.servicenow.servicenow_client import (
    ServiceNowAPIError,
    ServiceNowClient,
    is_sys_id,
    normalize_instance_url,
)
from routes.servicenow.tasks import (
    MAX_CLOSE_CODE,
    PREF_RCA_MODE,
    PREF_RCA_RULES,
    PREF_RESOLVE_CLOSE_CODE,
    PREF_RESOLVE_SYNC,
    PREF_WORK_NOTES,
    PREF_WRITE_MODE,
    RCA_MODES,
    WRITE_MODES,
    get_rca_mode,
    get_rca_rules,
    get_rca_settings,
    process_servicenow_event,
)
from routes.servicenow.webhook_auth import (
    WEBHOOK_SECRET_HEADER,
    new_webhook_secret,
    secret_matches,
)
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request, resolve_org_id, set_rls_context
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.db.db_utils import connect_to_db_as_user
from utils.log_sanitizer import sanitize
from utils.secrets.secret_ref_utils import delete_user_secret
from utils.web.public_url import external_backend_url

logger = logging.getLogger(__name__)

servicenow_bp = Blueprint("servicenow", __name__)

PROVIDER = "servicenow"
AUTH_TYPES = ("basic", "oauth_client_credentials")
_SAFE_LOG_RE = re.compile(r"[^a-zA-Z0-9._\-]")
_USER_ID_RE = re.compile(r"^[A-Za-z0-9\-]{1,64}$")
_MAX_FIELD = 512


_ERR_NOT_CONNECTED = "ServiceNow is not connected"


def _log_safe(value: Any, max_len: int = 80) -> str:
    return _SAFE_LOG_RE.sub("", str(value))[:max_len]


def _public_error(exc: Exception, fallback: str) -> str:
    """Fixed, user-facing text for a failed ServiceNow call; the exception itself stays in the logs."""
    if isinstance(exc, ServiceNowAPIError):
        if exc.status_code in (401, 403):
            return f"ServiceNow rejected the credentials (HTTP {exc.status_code})"
        if exc.status_code:
            return f"ServiceNow returned HTTP {exc.status_code}"
        return "Could not reach ServiceNow (timeout or network error)"
    return fallback


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
        # The credentials are still stored, so the connection stays connected and the
        # auth page keeps the webhook and RCA settings; the probe failure rides on `error`.
        logger.warning("[SERVICENOW] Status check failed for user %s: %s", user_id, exc)
        payload["error"] = _public_error(exc, "Stored ServiceNow credentials are incomplete; reconnect ServiceNow")
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
        logger.info("[SERVICENOW] Rejected instance URL for user %s: %s", sanitize(user_id), exc)
        return jsonify({
            "error": "Instance URL must be a plain https ServiceNow host, for example https://acme.service-now.com",
        }), 400

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
        return jsonify({"error": _public_error(exc, "Could not validate the ServiceNow connection")}), http_status

    existing = _stored_credentials(user_id) or {}
    token_payload: Dict[str, Any] = {
        "instance_url": instance_url,
        "auth_type": auth_type,
        "display_name": display_name,
        "validated_at": datetime.now(timezone.utc).isoformat(),
        # A reconnect keeps the secret so the customer's Business Rule keeps working.
        "webhook_secret": existing.get("webhook_secret") or new_webhook_secret(),
        # Write access is assumed until ServiceNow refuses a write (403), which clears it.
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
    """Webhook URL, shared secret and the setup steps for this user (the script ships with the client)."""
    creds = _stored_credentials(user_id)
    if not creds:
        return jsonify({"error": _ERR_NOT_CONNECTED}), 400

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
            (
                "In ServiceNow, open System Properties and create two properties: "
                "x_aurora.webhook_url (the URL above) and x_aurora.webhook_secret (the secret above, type password)."
            ),
            "Open System Definition > Business Rules and click New.",
            (
                "Table: Incident [incident]. Tick Advanced. When: Async. Tick Insert and Update "
                "(Update keeps the Aurora incident in sync when the ServiceNow incident changes state; "
                "Async means a slow Aurora never delays saving the incident)."
            ),
            "Paste the script below into the Script tab and save.",
            (
                "Choose which incidents Aurora investigates in the Automatic RCA settings, then create a "
                "test incident. It appears on the Aurora Incidents page within seconds."
            ),
        ],
    })


@servicenow_bp.route("/webhook-secret/rotate", methods=["POST"])
@require_permission("connectors", "write")
def rotate_webhook_secret(user_id):
    """Issue a new shared secret; the old one stops working immediately."""
    creds = _stored_credentials(user_id)
    if not creds:
        return jsonify({"error": _ERR_NOT_CONNECTED}), 400
    creds["webhook_secret"] = new_webhook_secret()
    try:
        store_tokens_in_db(user_id, creds, PROVIDER)
    except Exception:
        logger.exception("[SERVICENOW] Failed to rotate webhook secret for user %s", user_id)
        return jsonify({"error": "Failed to rotate webhook secret"}), 500
    logger.info("[SERVICENOW] Webhook secret rotated for user %s", user_id)
    return jsonify({"success": True, "webhookSecret": creds["webhook_secret"]})


# ── inbound webhook (no session; authenticated by the shared secret) ──


def _read_rca_settings(user_id: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Strict reads: a failure raises instead of looking like "no rules"."""
    rca_mode = get_rca_mode(user_id, strict=True)
    # Auto ignores the filters, so it doesn't need them.
    if rca_mode == "auto":
        return rca_mode, []
    return rca_mode, get_rca_rules(user_id, strict=True)


@servicenow_bp.route("/webhook/<user_id>", methods=["POST"])
def webhook(user_id: str):
    """Receive an incident record from the customer's Business Rule.

    A new incident is accepted only when the org's filters (or the Auto-mode
    judge) say so; an update to an incident Aurora already tracks is always
    queued so its state stays in sync.
    """
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

    event_kind = str(payload.get("event") or "inserted").lower()
    number = _log_safe(payload.get("number", ""))

    # Automatic RCA is opt-in. Asking Aurora to investigate a named incident does not go through here.
    try:
        rca_mode, rules = _read_rca_settings(user_id)
    except Exception:
        # A failed read must not look like "no rules"; the Business Rule logs the 503.
        logger.exception("[SERVICENOW] Could not read RCA settings for user %s", log_uid)
        return jsonify({"error": "could not read RCA settings"}), 503

    decision = decide_webhook(payload, rules, rca_mode)
    action = decision["action"]
    scope = decision.get("scope") or {}
    if action == "ignore" and event_kind == "inserted":
        # Nothing to sync and nothing to investigate: no incident, no queue.
        logger.info(
            "[SERVICENOW] Ignored %s (priority=%s group=%s category=%s reason=%s)",
            number, _log_safe(scope.get("priority") or "?"), _log_safe(scope.get("assignmentGroup") or "?"),
            _log_safe(scope.get("category") or "?"), _log_safe(decision.get("reason") or "filters"),
        )
        return jsonify({"received": True, "status": "ignored", "reason": decision.get("reason") or "filters"})

    logger.info(
        "[SERVICENOW] Webhook received: user=%s number=%s event=%s action=%s",
        log_uid, number, _log_safe(event_kind), _log_safe(action),
    )
    try:
        process_servicenow_event.delay(payload, user_id, decision=decision)
    except Exception:
        # Broker failure is on our side: 503 so the Business Rule logs it as a retryable error.
        logger.exception("[SERVICENOW] Failed to enqueue webhook for user %s", log_uid)
        return jsonify({"error": "could not enqueue webhook for processing"}), 503

    return jsonify({"received": True, "status": "accepted", "action": action})


# ── RCA settings ─────────────────────────────────────────────────────


def _store_settings(user_id: str, updates: List[Tuple[str, Any]]) -> bool:
    """Write all supplied ServiceNow prefs in one transaction (org-wide, like Jira)."""
    if not updates:
        return True
    conn = None
    cursor = None
    try:
        conn = connect_to_db_as_user()
        cursor = conn.cursor()
        org_id = set_rls_context(cursor, conn, user_id, log_prefix="[SERVICENOW:Settings]")
        if not org_id:
            return False
        for key, value in updates:
            cursor.execute(
                "DELETE FROM user_preferences WHERE org_id = %s AND preference_key = %s",
                (org_id, key),
            )
            cursor.execute(
                """
                INSERT INTO user_preferences (user_id, org_id, preference_key, preference_value)
                VALUES (%s, %s, %s, %s)
                """,
                (user_id, org_id, key, json.dumps(value)),
            )
        conn.commit()
        return True
    except Exception:
        logger.exception("[SERVICENOW] Failed to store settings for user %s", sanitize(user_id))
        if conn:
            conn.rollback()
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def _parse_settings_update(data: Dict[str, Any]) -> Tuple[List[Tuple[str, Any]], Optional[str]]:
    """Validated (pref key, value) pairs to store, or an error message for a 400."""
    updates: List[Tuple[str, Any]] = []
    known = ("rcaMode", "rcaRules", "workNotesEnabled", "writeMode", "resolveSync", "resolveCloseCode")
    if not any(key in data for key in known):
        return [], "rcaMode, rcaRules, workNotesEnabled, writeMode, resolveSync, or resolveCloseCode is required"

    if "rcaMode" in data:
        if data["rcaMode"] not in RCA_MODES:
            return [], f"rcaMode must be one of: {', '.join(RCA_MODES)}"
        updates.append((PREF_RCA_MODE, data["rcaMode"]))
    if "rcaRules" in data:
        rules, error = normalize_rca_rules(data.get("rcaRules"))
        if error:
            return [], error
        updates.append((PREF_RCA_RULES, rules))
    if "workNotesEnabled" in data:
        if not isinstance(data["workNotesEnabled"], bool):
            return [], "workNotesEnabled must be a boolean"
        updates.append((PREF_WORK_NOTES, data["workNotesEnabled"]))
        if data["workNotesEnabled"] is False:
            # Off means off: drop the write scope and the resolve sync with it, so a
            # later re-enable starts from notes only instead of a remembered full scope.
            updates.append((PREF_WRITE_MODE, "notes_only"))
            updates.append((PREF_RESOLVE_SYNC, False))
    if "writeMode" in data:
        if data["writeMode"] not in WRITE_MODES:
            return [], f"writeMode must be one of: {', '.join(WRITE_MODES)}"
        updates.append((PREF_WRITE_MODE, data["writeMode"]))
    if "resolveSync" in data:
        if not isinstance(data["resolveSync"], bool):
            return [], "resolveSync must be a boolean"
        updates.append((PREF_RESOLVE_SYNC, data["resolveSync"]))
    if "resolveCloseCode" in data:
        code = data["resolveCloseCode"]
        if not isinstance(code, str):
            return [], "resolveCloseCode must be text"
        code = " ".join(code.split())
        if not code or len(code) > MAX_CLOSE_CODE:
            return [], f"resolveCloseCode must be 1–{MAX_CLOSE_CODE} characters"
        updates.append((PREF_RESOLVE_CLOSE_CODE, code))
    return updates, None


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
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON body"}), 400

    updates, error = _parse_settings_update(data)
    if error:
        return jsonify({"error": error}), 400
    # Turning writes on only makes sense with an instance to write to.
    wants_write = data.get("workNotesEnabled") is True or data.get("resolveSync") is True
    if wants_write and not _stored_credentials(user_id):
        return jsonify({"error": "Connect ServiceNow before enabling write-back"}), 400

    if not _store_settings(user_id, updates):
        return jsonify({"error": "Failed to store ServiceNow settings"}), 500

    # Auto needs its policy memory; create it now so the card shows it right away.
    if data.get("rcaMode") == "auto":
        from services.memory.servicenow_memory import seed_servicenow_memory
        # Request org, not the TTL-cached users.org_id, so a just-changed org is seeded correctly.
        seed_servicenow_memory(user_id, org_id=get_org_id_from_request())

    # Write-back prefs decide which tools are bound; drop every member's cached tool list.
    from chat.backend.agent.tools.mcp_tools import clear_credentials_cache, clear_credentials_cache_for_org

    org_id = resolve_org_id(user_id)
    if org_id:
        clear_credentials_cache_for_org(org_id)
    else:
        clear_credentials_cache(user_id)

    logger.info(
        "[SERVICENOW] Updated settings for user %s: %s",
        sanitize(user_id), sanitize(", ".join(key for key, _ in updates)),
    )
    payload = get_rca_settings(user_id)
    payload["success"] = True
    return jsonify(payload)


# ── option catalog for the filter pickers ────────────────────────────


@servicenow_bp.route("/field-options", methods=["GET"])
@require_permission("connectors", "read")
def field_options(user_id):
    """Priorities, states, assignment groups and categories the rules editor can pick from."""
    creds = _stored_credentials(user_id)
    if not creds:
        return jsonify({"error": _ERR_NOT_CONNECTED}), 400
    try:
        client = ServiceNowClient.from_credentials(creds)
    except ValueError as exc:
        logger.warning("[SERVICENOW] Stored credentials unusable for user %s: %s", sanitize(user_id), exc)
        return jsonify({"error": "Stored ServiceNow credentials are incomplete; reconnect ServiceNow"}), 400
    return jsonify(build_field_options(client))
