"""
Onboarding routes for connector selection and setup flow.
"""
import logging
from flask import Blueprint, request, jsonify
from werkzeug.exceptions import BadRequest
from utils.auth.rbac_decorators import require_auth_only
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

onboarding_bp = Blueprint('onboarding', __name__)


@onboarding_bp.route('/complete', methods=['POST'])
@require_auth_only
def complete_onboarding(user_id):
    """Save connector selections and mark onboarding complete."""
    try:
        try:
            data = request.get_json()
        except BadRequest:
            return jsonify({"error": "Invalid JSON body"}), 400
        if not isinstance(data, dict):
            return jsonify({"error": "Invalid request body"}), 400

        selected_connectors = data.get("selected_connectors", [])

        if not isinstance(selected_connectors, list) or len(selected_connectors) > 50:
            return jsonify({"error": "Invalid connector selections"}), 400
        selected_connectors = [
            str(c)[:100] for c in selected_connectors if isinstance(c, str)
        ]

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT org_id FROM users WHERE id = %s", (user_id,))
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "User not found"}), 404
                org_id = row[0]

                if not org_id:
                    return jsonify({"error": "User is not associated with an organization"}), 400

                cur.execute(
                    """INSERT INTO onboarding_selections (org_id, user_id, selected_connectors)
                       VALUES (%s, %s, %s)
                       ON CONFLICT (org_id) DO UPDATE
                       SET selected_connectors = EXCLUDED.selected_connectors,
                           user_id = EXCLUDED.user_id""",
                    (org_id, user_id, selected_connectors),
                )

                cur.execute(
                    "UPDATE organizations SET onboarding_completed = TRUE WHERE id = %s",
                    (org_id,),
                )

                conn.commit()

        return jsonify({"success": True, "selected": selected_connectors})
    except Exception:
        logger.exception("Error completing onboarding")
        return jsonify({"error": "Internal server error"}), 500
