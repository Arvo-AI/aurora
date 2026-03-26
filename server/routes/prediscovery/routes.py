"""
Prediscovery API Routes

Provides endpoints to trigger and check status of infrastructure prediscovery.
"""

import logging

from flask import Blueprint, jsonify

from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

prediscovery_bp = Blueprint("prediscovery", __name__)


@prediscovery_bp.route("/run", methods=["POST"])
@require_permission("connectors", "write")
def trigger_prediscovery(user_id):
    """Trigger an on-demand prediscovery run for the current user."""

    try:
        from chat.background.prediscovery_task import run_prediscovery
        result = run_prediscovery.delay(user_id=user_id, trigger="manual")

        return jsonify({
            "status": "started",
            "task_id": result.id,
            "message": "Prediscovery started in background",
        }), 202
    except Exception as e:
        logger.exception(f"[Prediscovery API] Failed to trigger: {e}")
        return jsonify({"error": "Failed to start discovery"}), 500


@prediscovery_bp.route("/status", methods=["GET"])
@require_permission("connectors", "read")
def get_prediscovery_status(user_id):
    """Get the status of the latest prediscovery run for this org."""

    try:
        org_id = get_org_id_from_request()
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SET myapp.current_user_id = %s", (user_id,))
                cur.execute("SET myapp.current_org_id = %s", (org_id or '',))
                conn.commit()

                cur.execute("""
                    SELECT id, status, created_at, updated_at
                    FROM chat_sessions
                    WHERE org_id = %s
                      AND ui_state->'triggerMetadata'->>'source' = 'prediscovery'
                    ORDER BY created_at DESC
                    LIMIT 1
                """, (org_id,))
                row = cur.fetchone()

        if not row:
            return jsonify({"status": "never_run", "last_run": None})

        session_status = row[1]
        # Treat stale in_progress as failed (task was killed mid-run)
        if session_status == "in_progress" and row[3]:
            from datetime import datetime, timedelta
            if datetime.now() - row[3] > timedelta(minutes=35):
                session_status = "failed"

        return jsonify({
            "status": session_status,
            "session_id": str(row[0]),
            "started_at": (row[2].isoformat() + "Z") if row[2] else None,
            "updated_at": (row[3].isoformat() + "Z") if row[3] else None,
        })
    except Exception as e:
        logger.exception(f"[Prediscovery API] Failed to get status: {e}")
        return jsonify({"error": "Failed to get discovery status"}), 500
