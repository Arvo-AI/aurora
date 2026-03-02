"""Admin routes for RBAC user and role management.

All endpoints require the ``(users, manage)`` permission (admin-only).
"""

import logging

from flask import Blueprint, request, jsonify

from utils.auth.rbac_decorators import require_permission
from utils.auth.enforcer import get_enforcer, reload_policies
from utils.db.db_utils import connect_to_db_as_user

logger = logging.getLogger(__name__)

admin_bp = Blueprint("admin", __name__, url_prefix="/api/admin")

VALID_ROLES = {"admin", "editor", "viewer"}


@admin_bp.route("/users", methods=["GET"])
@require_permission("users", "manage")
def list_users(user_id):
    """List all users with their current role."""
    conn = connect_to_db_as_user()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, email, name, role, created_at FROM users ORDER BY created_at"
            )
            rows = cur.fetchall()
        return jsonify([
            {
                "id": r[0],
                "email": r[1],
                "name": r[2],
                "role": r[3] or "viewer",
                "created_at": r[4].isoformat() if r[4] else None,
            }
            for r in rows
        ]), 200
    finally:
        conn.close()


@admin_bp.route("/users/<target_user_id>/roles", methods=["GET"])
@require_permission("users", "manage")
def get_user_roles(user_id, target_user_id):
    """Get the roles assigned to a specific user."""
    enforcer = get_enforcer()
    roles = enforcer.get_roles_for_user(target_user_id)
    return jsonify({"user_id": target_user_id, "roles": roles}), 200


@admin_bp.route("/users/<target_user_id>/roles", methods=["POST"])
@require_permission("users", "manage")
def assign_role(user_id, target_user_id):
    """Assign a role to a user.

    Body: ``{ "role": "editor" }``
    """
    data = request.get_json(silent=True) or {}
    role = data.get("role", "").strip().lower()

    if role not in VALID_ROLES:
        return jsonify({"error": f"Invalid role. Must be one of: {', '.join(sorted(VALID_ROLES))}"}), 400

    enforcer = get_enforcer()

    # Remove any existing role assignments for this user
    current_roles = enforcer.get_roles_for_user(target_user_id)
    for old_role in current_roles:
        enforcer.remove_grouping_policy(target_user_id, old_role)

    enforcer.add_grouping_policy(target_user_id, role)
    enforcer.save_policy()
    reload_policies()

    # Keep the convenience column in sync
    conn = connect_to_db_as_user()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET role = %s WHERE id = %s", (role, target_user_id))
        conn.commit()
    finally:
        conn.close()

    logger.info("User %s assigned role '%s' by admin %s", target_user_id, role, user_id)
    return jsonify({"user_id": target_user_id, "role": role}), 200


@admin_bp.route("/users/<target_user_id>/roles/<role>", methods=["DELETE"])
@require_permission("users", "manage")
def revoke_role(user_id, target_user_id, role):
    """Revoke a specific role from a user, falling back to viewer."""
    role = role.strip().lower()
    if role not in VALID_ROLES:
        return jsonify({"error": f"Invalid role. Must be one of: {', '.join(sorted(VALID_ROLES))}"}), 400

    enforcer = get_enforcer()
    enforcer.remove_grouping_policy(target_user_id, role)

    # Ensure the user always has at least viewer
    remaining = enforcer.get_roles_for_user(target_user_id)
    if not remaining:
        enforcer.add_grouping_policy(target_user_id, "viewer")

    enforcer.save_policy()
    reload_policies()

    fallback_role = (enforcer.get_roles_for_user(target_user_id) or ["viewer"])[0]

    conn = connect_to_db_as_user()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET role = %s WHERE id = %s", (fallback_role, target_user_id))
        conn.commit()
    finally:
        conn.close()

    logger.info("Role '%s' revoked from user %s by admin %s (now: %s)",
                role, target_user_id, user_id, fallback_role)
    return jsonify({"user_id": target_user_id, "role": fallback_role}), 200
