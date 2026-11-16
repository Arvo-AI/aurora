import logging
from typing import Tuple

from flask import Blueprint, jsonify, request

from routes.grafana.tasks import process_grafana_alert
from utils.db.connection_pool import db_pool
from utils.web.public_url import external_backend_url
from utils.log_sanitizer import sanitize
from utils.auth.token_management import store_tokens_in_db
from utils.auth.rbac_decorators import require_permission
from utils.web.limiter_ext import limiter
from utils.auth.stateless_auth import get_org_id_from_request, validate_user_exists, set_rls_context

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

    return jsonify({"connected": True})


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

    Auto-creates or re-activates a connection record when needed.
    Always stores the alert; skips RCA for connection webhooks.
    """
    if not user_id:
        logger.warning("[GRAFANA] Webhook received without user_id")
        return jsonify({"error": "user_id is required"}), 400

    if not validate_user_exists(user_id):
        return jsonify({"error": "Unknown user"}), 404

    row_exists, is_active = _has_grafana_row(user_id)

    payload = request.get_json(silent=True) or {}
    skip_rca = False

    if not row_exists or (row_exists and not is_active):
        skip_rca = True
        if not row_exists:
            logger.info("[GRAFANA] Auto-connecting user %s via webhook", sanitize(user_id))
            try:
                store_tokens_in_db(user_id, {}, "grafana")
            except Exception:
                logger.exception("[GRAFANA] Failed to auto-connect user %s", sanitize(user_id))
                return jsonify({"error": "Failed to create Grafana connection"}), 500
        else:
            reactivated = _set_grafana_active(user_id, True)
            if not reactivated:
                logger.warning("[GRAFANA] Failed to re-activate connection for user %s via webhook", sanitize(user_id))
            else:
                logger.info("[GRAFANA] Re-activated connection for user %s via webhook", sanitize(user_id))

    logger.info("[GRAFANA] Received alert webhook for user %s: %s", sanitize(user_id), sanitize(payload.get("title", "unknown")))

    metadata = {
        "headers": dict(request.headers),
        "remote_addr": request.remote_addr,
    }

    process_grafana_alert.delay(payload, metadata, user_id, skip_rca=skip_rca)

    return jsonify({"received": True})


@grafana_bp.route("/alerts/preview", methods=["POST"])
@require_permission("incidents", "read")
def preview_alert(user_id):
    """Preview pasted Grafana JSON; no connector, database write, or RCA needed."""
    from services.grafana_alert_preview import preview_grafana_alert

    if request.content_length is not None and request.content_length > 256_000:
        return jsonify({"error": "Payload too large (256 KB maximum)."}), 413
    body = request.get_json(silent=True)
    # Backward-compatible: accept either the original raw Grafana payload,
    # or {"grafana_alert": <Grafana payload>, "error_log": <pasted text>}.
    if isinstance(body, dict) and "grafana_alert" in body:
        payload = body["grafana_alert"]
        error_log = body.get("error_log", "")
    else:
        payload = body
        error_log = ""
    try:
        result = preview_grafana_alert(payload, error_log=error_log)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(result), 200


@grafana_bp.route("/alerts/preview/analyze", methods=["POST"])
@limiter.limit("5 per hour")
@require_permission("chat", "write")
def analyze_pasted_alert(user_id):
    """Opt-in, read-only AI analysis of pasted alert + log evidence.

    Does not create an incident, connect Grafana, or execute RCA actions.
    The result is delivered asynchronously in the existing chat session UI.
    """
    from services.grafana_alert_preview import preview_grafana_alert
    from services.grafana_preview_analysis import build_preview_analysis_prompt
    from utils.auth.stateless_auth import get_org_id_from_request
    from utils.auth.viewer_run import viewer_may_run

    org_id = get_org_id_from_request()
    if org_id and not viewer_may_run(user_id, org_id):
        return jsonify({"error": "Chat isn't enabled for viewers in this organization."}), 403
    if request.content_length is not None and request.content_length > 256_000:
        return jsonify({"error": "Payload too large (256 KB maximum)."}), 413
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or body.get("confirm_analysis") is not True:
        return jsonify({"error": "Set confirm_analysis to true to start read-only AI analysis."}), 400
    if "grafana_alert" not in body:
        return jsonify({"error": "'grafana_alert' is required."}), 400
    try:
        preview = preview_grafana_alert(body["grafana_alert"], error_log=body.get("error_log", ""))
        prompt, rail_text = build_preview_analysis_prompt(preview, body.get("alert_index", 0))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    # Import lazily so the pure preview remains usable without Celery or DB.
    from chat.background.task import create_background_chat_session, run_background_chat
    index = body.get("alert_index", 0)
    selected = preview["previews"][index]
    metadata = {"source": "pasted_grafana_preview", "mode": "ask", "alert_index": index}
    try:
        session_id = create_background_chat_session(
            user_id=user_id,
            title=f"Alert preview: {selected['title']}"[:100],
            trigger_metadata=metadata,
        )
        task = run_background_chat.delay(
            user_id=user_id,
            session_id=session_id,
            initial_message=prompt,
            trigger_metadata=metadata,
            provider_preference=None,
            incident_id=None,  # Important: do NOT start the RCA incident workflow.
            send_notifications=False,
            mode="ask",  # Aurora's read-only agent mode.
            rail_text=rail_text,
        )
    except Exception:
        logger.exception("[GRAFANA] Could not schedule pasted-alert analysis")
        return jsonify({"error": "Failed to start analysis; please retry."}), 503
    return jsonify({
        "session_id": session_id,
        "task_id": str(task.id),
        "status": "queued",
        "mode": "ask",
        "note": "First-pass analysis from unverified pasted evidence; not a verified RCA.",
    }), 202


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
@require_permission("connectors", "read")
def get_webhook_url(user_id):
    """Get the webhook URL that should be configured in Grafana.

    When credentials are org-shared, returns the URL belonging to the token
    owner so org members see the same webhook URL without needing their own row.
    """
    from utils.secrets.secret_ref_utils import get_token_owner_id
    webhook_owner_id = get_token_owner_id(user_id, "grafana")

    base_url = external_backend_url()

    webhook_url = f"{base_url}/grafana/alerts/webhook/{webhook_owner_id}"

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
