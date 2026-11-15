import logging
from typing import Tuple

from flask import Blueprint, jsonify, request

from routes.grafana.tasks import process_grafana_alert
from utils.db.connection_pool import db_pool
from utils.web.public_url import external_backend_url
from utils.log_sanitizer import sanitize
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request, validate_user_exists, set_rls_context
from utils.web.webhook_auth import (
    WEBHOOK_HEADER_NAME,
    redact_webhook_headers,
    reject_unauthenticated_webhook,
    webhook_url_with_secret,
    with_webhook_secret,
)

logger = logging.getLogger(__name__)

grafana_bp = Blueprint("grafana", __name__)


def _has_grafana_row(user_id: str) -> Tuple[bool, bool]:
    """Check if a user_tokens row exists for Grafana (regardless of is_active).

    Returns (row_exists, is_active).
    """
    try:
        from utils.db.org_scope import resolve_org, org_read_predicate
        org_id = resolve_org(user_id)
        predicate, pred_params = org_read_predicate(user_id, org_id)
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix="[GRAFANA:has_row]")
                cursor.execute(
                    f"SELECT is_active FROM user_tokens WHERE {predicate} AND provider = 'grafana' ORDER BY is_active DESC LIMIT 1",
                    (*pred_params,),
                )
                row = cursor.fetchone()
                if row is None:
                    return False, False
                return True, bool(row[0])
    except Exception as exc:
        logger.error("[GRAFANA] Failed to check user_tokens row: %s", exc)
        return False, False


def _set_grafana_active(user_id: str, active: bool) -> bool:
    """Flip is_active on the existing Grafana user_tokens row."""
    try:
        from utils.db.org_scope import resolve_org, org_read_predicate
        org_id = resolve_org(user_id)
        predicate, pred_params = org_read_predicate(user_id, org_id)
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix="[GRAFANA:set_active]")
                cursor.execute(
                    f"UPDATE user_tokens SET is_active = %s, timestamp = CURRENT_TIMESTAMP "
                    f"WHERE {predicate} AND provider = 'grafana'",
                    (active, *pred_params),
                )
                updated = cursor.rowcount > 0
            conn.commit()
            return updated
    except Exception as exc:
        logger.error("[GRAFANA] Failed to set is_active=%s: %s", active, exc)
        return False


@grafana_bp.route("/status", methods=["GET"])
@require_permission("connectors", "read")
def status(user_id):
    row_exists, is_active = _has_grafana_row(user_id)

    if not row_exists or not is_active:
        return jsonify({"connected": False})

    creds = get_token_data(user_id, "grafana")
    return jsonify({"connected": True, "hasWebhookSecret": bool(creds.get("webhook_secret"))})


@grafana_bp.route("/disconnect", methods=["POST", "DELETE"])
@require_permission("connectors", "write")
def disconnect(user_id):
    """Disconnect Grafana by deactivating the stored connection."""
    try:
        row_exists, is_active = _has_grafana_row(user_id)
        if not row_exists:
            return jsonify({"success": True, "message": "No connection to disconnect"}), 200

        if not is_active:
            return jsonify({"success": True, "message": "Already disconnected"}), 200

        if _set_grafana_active(user_id, False):
            logger.info("[GRAFANA] Disconnected for user %s", user_id)
            return jsonify({"success": True, "message": "Grafana disconnected successfully"}), 200

        return jsonify({"error": "Failed to disconnect Grafana"}), 500
    except Exception as exc:
        logger.exception("[GRAFANA] Failed to disconnect provider")
        return jsonify({"error": "Failed to disconnect Grafana"}), 500


@grafana_bp.route("/alerts/webhook/<user_id>", methods=["POST"])
def alert_webhook(user_id: str):
    """Receive alert webhook from Grafana for a specific user.

    Authenticated by the connection's webhook secret. A request never creates
    or re-activates a connection: that happens from the authenticated
    webhook-url endpoint.
    """
    if not user_id:
        logger.warning("[GRAFANA] Webhook received without user_id")
        return jsonify({"error": "user_id is required"}), 400

    if not validate_user_exists(user_id):
        return jsonify({"error": "Unknown user"}), 404

    row_exists, is_active = _has_grafana_row(user_id)
    if not row_exists or not is_active:
        logger.warning("[GRAFANA] Webhook received for user %s with no Grafana connection", sanitize(user_id))
        return jsonify({"error": "Grafana not connected for this user"}), 404

    rejection = reject_unauthenticated_webhook(
        get_token_data(user_id, "grafana"), request,
        provider_name="Grafana", log_prefix="[GRAFANA]", user_id=user_id,
    )
    if rejection:
        return rejection

    payload = request.get_json(silent=True) or {}

    logger.info("[GRAFANA] Received alert webhook for user %s: %s", sanitize(user_id), sanitize(payload.get("title", "unknown")))

    metadata = {
        "headers": redact_webhook_headers(request.headers),
        "remote_addr": request.remote_addr,
    }

    process_grafana_alert.delay(payload, metadata, user_id)

    return jsonify({"received": True})


@grafana_bp.route("/alerts", methods=["GET"])
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
            set_rls_context(cursor, conn, user_id, log_prefix="[Grafana]")
            
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
    except Exception:
        logger.exception("[GRAFANA] Failed to fetch alerts for user %s", sanitize(user_id))
        return jsonify({"error": "Failed to fetch alerts"}), 500


@grafana_bp.route("/alerts/webhook-url", methods=["GET"])
@require_permission("connectors", "write")
def get_webhook_url(user_id):
    """Get the webhook URL (with its secret) that should be configured in Grafana.

    Grafana has no API credentials to connect with, so the first call creates
    the connection and its webhook secret. Requires ``connectors:write``
    because the response reveals the secret.

    When credentials are org-shared, returns the URL belonging to the token
    owner so org members see the same webhook URL without needing their own row.
    """
    from utils.secrets.secret_ref_utils import get_token_owner_id

    creds = get_token_data(user_id, "grafana")
    webhook_secret = creds.get("webhook_secret")
    if not webhook_secret:
        payload = with_webhook_secret(creds, None)
        try:
            store_tokens_in_db(get_token_owner_id(user_id, "grafana"), payload, "grafana")
        except Exception:
            logger.exception("[GRAFANA] Failed to create webhook secret for user %s", sanitize(user_id))
            return jsonify({"error": "Failed to create Grafana webhook secret"}), 500
        webhook_secret = payload["webhook_secret"]
        logger.info("[GRAFANA] Created connection and webhook secret for user %s", sanitize(user_id))

    webhook_owner_id = get_token_owner_id(user_id, "grafana")

    base_url = external_backend_url()

    webhook_url = f"{base_url}/grafana/alerts/webhook/{webhook_owner_id}"

    return jsonify({
        "webhookUrl": webhook_url_with_secret(webhook_url, webhook_secret),
        "webhookSecret": webhook_secret,
        "headerName": WEBHOOK_HEADER_NAME,
        "instructions": [
            "1. Go to your Grafana instance",
            "2. Navigate to Alerts & IRM > Notification Configuration > Contact points",
            "3. Click New contact point",
            "4. Select 'Webhook' as the integration type",
            "5. Paste the webhook URL above into the URL field (it includes your webhook secret; treat it like a password)",
            "6. Click Test to send a test notification",
            "7. Save the contact point and add it to your notification policies"
        ]
    })
