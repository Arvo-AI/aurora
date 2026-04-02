import logging
import os
from typing import Any, Dict, Optional, Tuple

from flask import Blueprint, jsonify, request

from routes.grafana.tasks import process_grafana_alert
from utils.db.connection_pool import db_pool
from utils.web.cors_utils import create_cors_response
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request

logger = logging.getLogger(__name__)

grafana_bp = Blueprint("grafana", __name__)


def _has_grafana_row(user_id: str) -> Tuple[bool, bool]:
    """Check if a user_tokens row exists for Grafana (regardless of is_active).

    Returns (row_exists, is_active).
    """
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT is_active FROM user_tokens WHERE user_id = %s AND provider = 'grafana' LIMIT 1",
                    (user_id,),
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
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE user_tokens SET is_active = %s, timestamp = CURRENT_TIMESTAMP "
                    "WHERE user_id = %s AND provider = 'grafana'",
                    (active, user_id),
                )
                updated = cursor.rowcount > 0
            conn.commit()
            return updated
    except Exception as exc:
        logger.error("[GRAFANA] Failed to set is_active=%s: %s", active, exc)
        return False


def _get_stored_grafana_credentials(user_id: str) -> Optional[Dict[str, Any]]:
    try:
        return get_token_data(user_id, "grafana")
    except Exception as exc:
        logger.error("Failed to retrieve Grafana credentials for user %s: %s", user_id, exc)
        return None


@grafana_bp.route("/status", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def status(user_id):
    row_exists, is_active = _has_grafana_row(user_id)

    if not row_exists or not is_active:
        return jsonify({"connected": False})

    creds = _get_stored_grafana_credentials(user_id)
    if not creds:
        return jsonify({"connected": False})

    base_url = creds.get("base_url")
    if not base_url:
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


@grafana_bp.route("/alerts/webhook/<user_id>", methods=["POST", "OPTIONS"])
def alert_webhook(user_id: str):
    """Receive alert webhook from Grafana for a specific user.

    Auto-creates or re-activates a connection record when needed.
    Always stores the alert; skips RCA for connection webhooks.
    """
    if request.method == "OPTIONS":
        return create_cors_response()

    if not user_id:
        logger.warning("[GRAFANA] Webhook received without user_id")
        return jsonify({"error": "user_id is required"}), 400

    row_exists, is_active = _has_grafana_row(user_id)

    payload = request.get_json(silent=True) or {}
    skip_rca = False

    if not row_exists or (row_exists and not is_active):
        skip_rca = True
        external_url = (payload.get("externalURL") or "").strip().rstrip("/")
        if not row_exists:
            base_url = external_url or "unknown"
            logger.info("[GRAFANA] Auto-connecting user %s via webhook (externalURL=%s)", user_id, base_url)
            try:
                store_tokens_in_db(user_id, {"base_url": base_url}, "grafana")
            except Exception as exc:
                logger.exception("[GRAFANA] Failed to auto-connect user %s: %s", user_id, exc)
                return jsonify({"error": "Failed to create Grafana connection"}), 500
        else:
            reactivated = False
            if external_url:
                try:
                    store_tokens_in_db(user_id, {"base_url": external_url}, "grafana")
                    reactivated = True
                except Exception:
                    reactivated = _set_grafana_active(user_id, True)
            else:
                reactivated = _set_grafana_active(user_id, True)
            if not reactivated:
                logger.warning("[GRAFANA] Failed to re-activate connection for user %s via webhook", user_id)
            else:
                logger.info("[GRAFANA] Re-activated connection for user %s via webhook", user_id)

    logger.info("[GRAFANA] Received alert webhook for user %s: %s", user_id, payload.get("title", "unknown"))

    metadata = {
        "headers": dict(request.headers),
        "remote_addr": request.remote_addr,
    }

    process_grafana_alert.delay(payload, metadata, user_id, skip_rca=skip_rca)

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
            "2. Navigate to Alerts & IRM > Notification Configuration > Contact points",
            "3. Click New contact point",
            "4. Select 'Webhook' as the integration type",
            "5. Paste the webhook URL above into the URL field",
            "6. Click Test to send a test notification",
            "7. Save the contact point and add it to your notification policies"
        ]
    })
