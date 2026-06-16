"""
Onboarding routes for connector selection and setup flow.
"""
import logging
from flask import Blueprint, request, jsonify
from utils.auth.rbac_decorators import require_permission
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

onboarding_bp = Blueprint('onboarding', __name__)


@onboarding_bp.route('/complete', methods=['POST'])
@require_permission("connectors", "write")
def complete_onboarding(user_id):
    """Save connector selections and mark onboarding complete."""
    try:
        data = request.get_json() or {}
        selected_connectors = data.get("selected_connectors", [])

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT org_id FROM users WHERE id = %s", (user_id,))
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "User not found"}), 404
                org_id = row[0]

                if selected_connectors:
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
    except Exception as e:
        logger.error(f"Error completing onboarding: {e}")
        return jsonify({"error": "Internal server error"}), 500
