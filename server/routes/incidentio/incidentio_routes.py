"""incident.io connector routes: connect, status, disconnect, webhook, alerts, settings."""

import base64
import hashlib
import hmac
import logging
import os
import re
from typing import Any, Dict, Optional

from flask import Blueprint, jsonify, request

from routes.incidentio.incidentio_client import (
    IncidentioAPIError,
    IncidentioClient,
    postback_can_enable,
)
from routes.incidentio.tasks import (
    process_incidentio_event,
    get_org_severities,
    get_postback_access,
    invalidate_org_severity_cache,
    invalidate_postback_access_cache,
    _get_org_severity_ranks,
)
from utils.db.connection_pool import db_pool
from utils.auth.stateless_auth import (
    get_user_preference,
    set_rls_context,
    store_user_preference,
)
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.auth.rbac_decorators import require_permission
from utils.secrets.secret_ref_utils import delete_user_secret

_SAFE_LOG_RE = re.compile(r"[^a-zA-Z0-9._\-]")

logger = logging.getLogger(__name__)

incidentio_bp = Blueprint("incidentio", __name__)


def _sanitize_for_log(value: str, max_len: int = 80) -> str:
    """Strip any character that isn't alphanumeric, dot, dash, or underscore."""
    return _SAFE_LOG_RE.sub("", value)[:max_len]


def _get_stored_credentials(user_id: str) -> Optional[Dict[str, Any]]:
    try:
        creds = get_token_data(user_id, "incidentio")
        return creds if creds else None
    except Exception:
        logger.exception("[INCIDENTIO] Failed to retrieve credentials for user %s", user_id)
        return None


# ── Routes ──────────────────────────────────────────────────────────


@incidentio_bp.route("/connect", methods=["POST"])
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
        logger.warning("[INCIDENTIO] Connection validation failed: %s", exc.code)
        return jsonify({"error": str(exc)}), 502

    token_payload = {"api_key": api_key}

    try:
        store_tokens_in_db(user_id, token_payload, "incidentio")
    except Exception:
        logger.exception("[INCIDENTIO] Failed to store credentials for user %s", user_id)
        return jsonify({"error": "Failed to store credentials"}), 500

    # New/rotated key may have different scopes — drop any cached catalog so
    # the next fetch reflects this key's actual permissions.
    invalidate_org_severity_cache(user_id)
    invalidate_postback_access_cache(user_id)

    return jsonify({"success": True, "connected": True})


@incidentio_bp.route("/status", methods=["GET"])
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
    except IncidentioAPIError:
        logger.warning("[INCIDENTIO] Status check failed for user %s", user_id)
        return jsonify({"connected": False, "error": "Connection check failed"})

    return jsonify({"connected": True})


@incidentio_bp.route("/disconnect", methods=["POST", "DELETE"])
@require_permission("connectors", "write")
def disconnect(user_id):
    """Remove stored incident.io credentials."""
    try:
        success, _ = delete_user_secret(user_id, "incidentio")
        if not success:
            return jsonify({"error": "Failed to delete stored credentials"}), 500

        # Clear cached severity catalog so a future reconnect re-fetches.
        invalidate_org_severity_cache(user_id)
        invalidate_postback_access_cache(user_id)

        logger.info("[INCIDENTIO] Disconnected user %s", user_id)
        return jsonify({"success": True, "message": "incident.io disconnected successfully"})
    except Exception:
        logger.exception("[INCIDENTIO] Failed to disconnect user %s", user_id)
        return jsonify({"error": "Failed to disconnect incident.io"}), 500


@incidentio_bp.route("/alerts/webhook/<user_id>", methods=["POST"])
def alert_webhook(user_id: str):
    """Receive webhook events from incident.io."""
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    log_uid = _sanitize_for_log(user_id, 36)

    creds = get_token_data(user_id, "incidentio")
    if not creds:
        logger.warning("[INCIDENTIO] Webhook with no connection: %s", log_uid)
        return jsonify({"error": "incident.io not connected for this user"}), 404

    webhook_secret = creds.get("webhook_secret")
    if webhook_secret:
        msg_id = request.headers.get("webhook-id", "")
        timestamp = request.headers.get("webhook-timestamp", "")
        signature_header = request.headers.get("webhook-signature", "")
        if not msg_id or not timestamp or not signature_header:
            logger.warning("[INCIDENTIO] Webhook rejected: missing Svix headers: %s", log_uid)
            return jsonify({"error": "Missing webhook signature headers"}), 401
        to_sign = f"{msg_id}.{timestamp}.{request.get_data(as_text=True)}"
        secret_bytes = base64.b64decode(webhook_secret.split("_")[-1]) if webhook_secret.startswith("whsec_") else webhook_secret.encode()
        expected = base64.b64encode(hmac.new(secret_bytes, to_sign.encode(), hashlib.sha256).digest()).decode()
        signatures = [s.split(",")[-1] for s in signature_header.split(" ")]
        if not any(hmac.compare_digest(expected, s) for s in signatures):
            logger.warning("[INCIDENTIO] Webhook rejected: invalid signature: %s", log_uid)
            return jsonify({"error": "Invalid webhook signature"}), 401

    payload = request.get_json(silent=True) or {}

    event_type = payload.get("event_type") or (payload.get("event", {}) or {}).get("type", "unknown")
    logger.info("[INCIDENTIO] Webhook received: user=%s event=%s", log_uid, _sanitize_for_log(str(event_type)))

    metadata = {"remote_addr": request.remote_addr}
    process_incidentio_event.delay(payload, metadata, user_id)

    return jsonify({"received": True})


@incidentio_bp.route("/alerts", methods=["GET"])
@require_permission("connectors", "read")
def get_alerts(user_id):
    """Fetch stored incident.io events."""
    limit = min(max(request.args.get("limit", 50, type=int), 1), 200)
    offset = max(request.args.get("offset", 0, type=int), 0)
    severity_filter = request.args.get("severity")

    try:
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()
            org_id = set_rls_context(cursor, conn, user_id, log_prefix="[INCIDENTIO:alerts]")

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
    except Exception:
        logger.exception("[INCIDENTIO] Failed to fetch alerts")
        return jsonify({"error": "Failed to fetch alerts"}), 500


@incidentio_bp.route("/alerts/webhook-url", methods=["GET"])
@require_permission("connectors", "read")
def get_webhook_url(user_id):
    """Return the webhook URL for this user's incident.io configuration."""
    ngrok_url = os.getenv("NGROK_URL", "").rstrip("/")
    backend_url = os.getenv("NEXT_PUBLIC_BACKEND_URL", "").rstrip("/")
    base_url = ngrok_url if ngrok_url and backend_url.startswith("http://localhost") else backend_url
    if not base_url:
        base_url = request.host_url.rstrip("/")

    webhook_url = f"{base_url}/incidentio/alerts/webhook/{user_id}"

    creds = _get_stored_credentials(user_id)
    has_secret = bool(creds and creds.get("webhook_secret"))

    return jsonify({
        "webhookUrl": webhook_url,
        "hasWebhookSecret": has_secret,
        "instructions": [
            "1. Go to incident.io → Settings → Webhooks",
            "2. Click 'Add endpoint'",
            "3. Paste the webhook URL above",
            "\n".join([
                "4. Subscribe to these event types (each is a separate subscription):",
                "    • public_incident.incident_created_v2  (required)",
                "    • public_alert.alert_created_v1  (required)",
                "    • private_incident.incident_created_v2  (only if you use private incidents)",
                "    • private_alert.alert_created_v1  (only if you use private alerts)",
            ]),
            "5. Save the endpoint, then copy the signing secret from the endpoint settings",
            "6. Paste the signing secret (starts with whsec_) into the field above",
        ],
    })


@incidentio_bp.route("/webhook-secret", methods=["PUT"])
@require_permission("connectors", "write")
def save_webhook_secret(user_id):
    """Store the webhook signing secret from incident.io's endpoint settings."""
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    secret = data.get("webhookSecret", "").strip()
    if not secret:
        return jsonify({"error": "webhookSecret is required"}), 400

    creds = _get_stored_credentials(user_id)
    if not creds:
        return jsonify({"error": "incident.io not connected"}), 400

    creds["webhook_secret"] = secret
    try:
        store_tokens_in_db(user_id, creds, "incidentio")
    except Exception:
        logger.exception("[INCIDENTIO] Failed to store webhook secret for user %s", user_id)
        return jsonify({"error": "Failed to store webhook secret"}), 500

    return jsonify({"success": True})


@incidentio_bp.route("/severities", methods=["GET"])
@require_permission("connectors", "read")
def list_org_severities(user_id):
    """Return the org's configured incident.io severities for the RCA filter UI.

    Shape: {"available": bool, "denied": bool, "severities": [{name, rank}]}.
    ``available`` is False (and ``denied`` True) when the API key lacks the
    "View data" scope, so the client can disable severity-name selection.
    """
    result = get_org_severities(user_id)
    return jsonify(result)


def _rca_settings_payload(
    user_id: str,
    access: Optional[Dict[str, Any]] = None,
    *,
    postback_refused: bool = False,
) -> Dict[str, Any]:
    if access is None:
        access = get_postback_access(user_id)
    payload = {
        "rcaEnabled": get_user_preference(user_id, "incidentio_rca_enabled", default=True),
        "postbackEnabled": get_user_preference(user_id, "incidentio_postback_enabled", default=False),
        "alertRcaEnabled": get_user_preference(user_id, "incidentio_alert_rca_enabled", default=True),
        "alertMinSeverity": get_user_preference(user_id, "incidentio_alert_min_severity", default="low"),
        "alertSeverityAllowlist": get_user_preference(
            user_id, "incidentio_alert_severity_allowlist", default=None
        ),
        "postbackAccess": access,
    }
    if postback_refused:
        payload["postbackRefused"] = True
    return payload


@incidentio_bp.route("/rca-settings", methods=["GET"])
@require_permission("connectors", "read")
def get_rca_settings(user_id):
    return jsonify(_rca_settings_payload(user_id))


# Severity labels accepted by the alert RCA filter (normalized values only).
_VALID_SEVERITIES = frozenset(("critical", "high", "medium", "low", "unknown"))


@incidentio_bp.route("/rca-settings", methods=["PUT"])
@require_permission("connectors", "write")
def update_rca_settings(user_id):
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    rca_enabled = data.get("rcaEnabled")
    postback_enabled = data.get("postbackEnabled")
    alert_rca_enabled = data.get("alertRcaEnabled")
    alert_min_severity = data.get("alertMinSeverity")
    alert_severity_allowlist = data.get("alertSeverityAllowlist")
    # Reused in the response so a refused enable doesn't call identity twice.
    access = None
    postback_refused = False

    if rca_enabled is not None:
        if not isinstance(rca_enabled, bool):
            return jsonify({"error": "rcaEnabled must be a boolean"}), 400
        store_user_preference(user_id, "incidentio_rca_enabled", rca_enabled)

    if postback_enabled is not None:
        if not isinstance(postback_enabled, bool):
            return jsonify({"error": "postbackEnabled must be a boolean"}), 400
        # Turning on with a key that can write nowhere would silently drop every RCA.
        if postback_enabled:
            access = get_postback_access(user_id)
            if not postback_can_enable(access):
                postback_refused = True
                logger.info("[INCIDENTIO] Post-back left off: API key cannot write incidents or alerts")
            else:
                store_user_preference(user_id, "incidentio_postback_enabled", value=True)
        else:
            store_user_preference(user_id, "incidentio_postback_enabled", value=False)

    if alert_rca_enabled is not None:
        if not isinstance(alert_rca_enabled, bool):
            return jsonify({"error": "alertRcaEnabled must be a boolean"}), 400
        store_user_preference(user_id, "incidentio_alert_rca_enabled", alert_rca_enabled)

    # Minimum-severity threshold: either a normalized bucket label OR a real
    # org severity name (so the UI can offer the user's actual severities).
    if alert_min_severity is not None:
        if not isinstance(alert_min_severity, str) or not alert_min_severity.strip():
            return jsonify({"error": "alertMinSeverity must be a non-empty string"}), 400
        candidate = alert_min_severity.lower().strip()
        # Accept a fixed bucket, or a real severity name from the org catalog.
        if candidate not in _VALID_SEVERITIES:
            org_ranks = _get_org_severity_ranks(user_id)
            if candidate not in org_ranks:
                return jsonify({
                    "error": (
                        "alertMinSeverity must be one of: "
                        f"{', '.join(sorted(_VALID_SEVERITIES))}, "
                        "or a severity configured in your incident.io organization"
                    )
                }), 400
        store_user_preference(user_id, "incidentio_alert_min_severity", candidate)

    # Explicit allowlist: list of normalized severities, or null/empty to clear
    # it (falls back to the minimum-severity threshold).
    if alert_severity_allowlist is not None:
        if not isinstance(alert_severity_allowlist, list):
            return jsonify({"error": "alertSeverityAllowlist must be a list or null"}), 400
        normalized = [str(s).lower() for s in alert_severity_allowlist]
        # Accept a fixed bucket, or a real severity name from the org catalog
        # (mirrors alertMinSeverity). Only resolve the org catalog for names
        # that aren't already a known fixed bucket to avoid a needless API call.
        unknown = [s for s in normalized if s not in _VALID_SEVERITIES]
        org_ranks = _get_org_severity_ranks(user_id) if unknown else {}
        invalid = [s for s in unknown if s not in org_ranks]
        if invalid:
            return jsonify({
                "error": (
                    "alertSeverityAllowlist contains invalid severities: "
                    f"{', '.join(invalid)}"
                )
            }), 400
        # Store an empty list as None so downstream treats it as "no allowlist".
        store_user_preference(
            user_id,
            "incidentio_alert_severity_allowlist",
            normalized or None,
        )

    return jsonify({"success": True, **_rca_settings_payload(user_id, access, postback_refused=postback_refused)})
