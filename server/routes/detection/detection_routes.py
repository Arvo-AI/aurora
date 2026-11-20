"""Read-only view of detection memory: what Aurora watches, why, and its record.

Checks are created and revised only by Aurora
(chat/backend/agent/tools/detection_check_tool.py), where the command is
validated and gated; a second path for that here would have to repeat all of
it. Stopping a check needs none of that, so an editor can do it from the page.
"""
import logging

from flask import jsonify, request

from services.detection.store import list_checks, retire_check_by_id
from utils.auth.enforcer import enforce_with_reload
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request, set_rls_context
from utils.db.connection_pool import db_pool
from utils.validation import is_valid_uuid

from . import detection_bp

logger = logging.getLogger(__name__)

_ERR_INTERNAL = "Internal server error"


@detection_bp.route("/checks", methods=["GET"])
@require_permission("detection_checks", "read")
def get_checks(user_id):
    include_retired = request.args.get("include_retired", "").lower() == "true"
    try:
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            # Pinned to the org RBAC just authorized: resolving it again from the
            # user could return a different one while a reassignment propagates.
            org_id = set_rls_context(
                cur, conn, user_id, org_id=get_org_id_from_request(), log_prefix="[Detection:list]",
            )
            if not org_id:
                return jsonify({"error": "No organization"}), 403
            checks = list_checks(cur, org_id, include_retired=include_retired)
    except Exception:
        logger.exception("[Detection] Failed to list checks")
        return jsonify({"error": _ERR_INTERNAL}), 500
    can_edit = enforce_with_reload(user_id, org_id, "detection_checks", "write")
    if not can_edit:
        # What a command printed can be more than a viewer may see elsewhere
        # (a ConfigMap read by someone with connector access, for instance).
        for check in checks:
            check.pop("last_observed", None)
    return jsonify({"checks": checks, "can_edit": can_edit})


@detection_bp.route("/checks/<check_id>/retire", methods=["POST"])
@require_permission("detection_checks", "write")
def retire(user_id, check_id):
    """Stop running a check. Its record is kept and its name is not reused."""
    if not is_valid_uuid(check_id):
        return jsonify({"error": "Check not found"}), 404
    reason = ((request.get_json(silent=True) or {}).get("reason") or "").strip()
    try:
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            org_id = set_rls_context(
                cur, conn, user_id, org_id=get_org_id_from_request(), log_prefix="[Detection:retire]",
            )
            if not org_id:
                return jsonify({"error": "No organization"}), 403
            retired = retire_check_by_id(cur, org_id, check_id, reason or "Stopped from the Checks page.")
            conn.commit()
    except Exception:
        logger.exception("[Detection] Failed to retire check")
        return jsonify({"error": _ERR_INTERNAL}), 500
    if not retired:
        return jsonify({"error": "Check not found"}), 404
    return jsonify({"status": "retired"})
