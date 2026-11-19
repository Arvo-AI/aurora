"""Read-only view of detection memory: what Aurora watches, why, and its record.

There is no write route on purpose. Checks are created, revised and retired by
Aurora (chat/backend/agent/tools/detection_check_tool.py), where the command is
validated and gated; a second write path here would have to repeat all of it.
"""
import logging

from flask import jsonify, request

from services.detection.store import list_checks
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import set_rls_context
from utils.db.connection_pool import db_pool

from . import detection_bp

logger = logging.getLogger(__name__)

_ERR_INTERNAL = "Internal server error"


@detection_bp.route("/checks", methods=["GET"])
@require_permission("detection_checks", "read")
def get_checks(user_id):
    include_retired = request.args.get("include_retired", "").lower() == "true"
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                org_id = set_rls_context(cur, conn, user_id, log_prefix="[Detection:list]")
                if not org_id:
                    return jsonify({"error": "No organization"}), 403
                checks = list_checks(cur, org_id, include_retired=include_retired)
    except Exception:
        logger.exception("[Detection] Failed to list checks")
        return jsonify({"error": _ERR_INTERNAL}), 500
    return jsonify({"checks": checks})
