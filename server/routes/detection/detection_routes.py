"""Read-only view of detection memory: what Aurora watches, why, and its record.

Checks are created and revised only by Aurora
(chat/backend/agent/tools/detection_check_tool.py), where the command is
validated and gated; a second path for that here would have to repeat all of
it. Stopping a check needs none of that, so an editor can do it from the page.
"""
import logging

from flask import jsonify, request

from services.detection import checks_enabled
from services.detection.ownership import resume_check
from services.detection.store import list_checks, retire_check_by_id, vouch_check
from utils.auth.enforcer import enforce_with_reload
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request, set_rls_context
from utils.db.connection_pool import db_pool
from utils.validation import is_valid_uuid

from . import detection_bp

logger = logging.getLogger(__name__)

_ERR_INTERNAL = "Internal server error"
_ERR_NO_ORG = "No organization"
_ERR_NOT_FOUND = "Check not found"


@detection_bp.route("/status", methods=["GET"])
@require_permission("detection_checks", "read")
def get_status(user_id):
    """Whether the feature is on for this install. The sidebar hides the page when it is not."""
    return jsonify({"enabled": checks_enabled()})


@detection_bp.route("/checks", methods=["GET"])
@require_permission("detection_checks", "read")
def get_checks(user_id):
    if not checks_enabled():
        return jsonify({"enabled": False, "checks": [], "can_edit": False})
    include_retired = request.args.get("include_retired", "").lower() == "true"
    try:
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            # Pinned to the org RBAC just authorized: resolving it again from the
            # user could return a different one while a reassignment propagates.
            org_id = set_rls_context(
                cur, conn, user_id, org_id=get_org_id_from_request(), log_prefix="[Detection:list]",
            )
            if not org_id:
                return jsonify({"error": _ERR_NO_ORG}), 403
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
    return jsonify({"enabled": True, "checks": checks, "can_edit": can_edit})


@detection_bp.route("/checks/<check_id>/retire", methods=["POST"])
@require_permission("detection_checks", "write")
def retire(user_id, check_id):
    """Stop running a check. Its record is kept and its name is not reused."""
    if not is_valid_uuid(check_id):
        return jsonify({"error": _ERR_NOT_FOUND}), 404
    reason = ((request.get_json(silent=True) or {}).get("reason") or "").strip()
    try:
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            org_id = set_rls_context(
                cur, conn, user_id, org_id=get_org_id_from_request(), log_prefix="[Detection:retire]",
            )
            if not org_id:
                return jsonify({"error": _ERR_NO_ORG}), 403
            retired = retire_check_by_id(cur, org_id, check_id, reason or "Stopped from the Checks page.")
            conn.commit()
    except Exception:
        logger.exception("[Detection] Failed to retire check")
        return jsonify({"error": _ERR_INTERNAL}), 500
    if not retired:
        return jsonify({"error": _ERR_NOT_FOUND}), 404
    return jsonify({"status": "retired"})


@detection_bp.route("/checks/<check_id>/vouch", methods=["POST"])
@require_permission("detection_checks", "write")
def vouch(user_id, check_id):
    """A person decides this check may open an incident when it fails."""
    if not is_valid_uuid(check_id):
        return jsonify({"error": _ERR_NOT_FOUND}), 404
    try:
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            org_id = set_rls_context(
                cur, conn, user_id, org_id=get_org_id_from_request(), log_prefix="[Detection:vouch]",
            )
            if not org_id:
                return jsonify({"error": _ERR_NO_ORG}), 403
            vouched = vouch_check(cur, org_id, check_id, user_id)
            conn.commit()
    except Exception:
        logger.exception("[Detection] Failed to vouch for check")
        return jsonify({"error": _ERR_INTERNAL}), 500
    if not vouched:
        # Not this org's, retired, or already trusted.
        return jsonify({"error": "Check not found or not waiting to be trusted"}), 404
    return jsonify({"status": "proven"})


@detection_bp.route("/checks/<check_id>/resume", methods=["POST"])
@require_permission("detection_checks", "write")
def resume(user_id, check_id):
    """Run a paused check again, as the person resuming it."""
    if not is_valid_uuid(check_id):
        return jsonify({"error": "Check not found"}), 404
    try:
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            org_id = set_rls_context(
                cur, conn, user_id, org_id=get_org_id_from_request(), log_prefix="[Detection:resume]",
            )
            if not org_id:
                return jsonify({"error": "No organization"}), 403
            resumed = resume_check(cur, org_id, check_id, user_id)
            conn.commit()
    except Exception:
        logger.exception("[Detection] Failed to resume check")
        return jsonify({"error": _ERR_INTERNAL}), 500
    if not resumed:
        return jsonify({"error": "Check not found or not paused"}), 404
    return jsonify({"status": "resumed"})
