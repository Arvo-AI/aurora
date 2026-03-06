"""Admin routes for RBAC user and role management.

All endpoints require the ``(users, manage)`` permission (admin-only).
"""

import logging

import bcrypt
from flask import Blueprint, request, jsonify

from utils.auth.rbac_decorators import require_permission
from utils.auth.enforcer import get_enforcer, reload_policies
from utils.auth.stateless_auth import get_org_id_from_request
from utils.db.db_utils import connect_to_db_as_user

logger = logging.getLogger(__name__)

admin_bp = Blueprint("admin", __name__, url_prefix="/api/admin")

from utils.auth import VALID_ROLES


@admin_bp.route("/users", methods=["GET"])
@require_permission("users", "manage")
def list_users(user_id):
    """List users within the caller's org."""
    org_id = get_org_id_from_request()
    conn = connect_to_db_as_user()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, email, name, role, created_at FROM users WHERE org_id = %s ORDER BY created_at",
                (org_id,),
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


@admin_bp.route("/users", methods=["POST"])
@require_permission("users", "manage")
def create_user(user_id):
    """Admin-created user with a specified role."""
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    name = (data.get("name") or "").strip()
    role = (data.get("role") or "viewer").strip().lower()

    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400
    if role not in VALID_ROLES:
        return jsonify({"error": f"Invalid role. Must be one of: {', '.join(sorted(VALID_ROLES))}"}), 400

    org_id = get_org_id_from_request()
    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    conn = connect_to_db_as_user()
    try:
        with conn.cursor() as cur:
            cur.execute("SET myapp.current_user_id = %s;", (user_id,))
            if org_id:
                cur.execute("SET myapp.current_org_id = %s;", (org_id,))
            conn.commit()

            cur.execute("SELECT id FROM users WHERE email = %s AND org_id = %s", (email, org_id))
            if cur.fetchone():
                return jsonify({"error": "User with this email already exists"}), 409

            cur.execute(
                """INSERT INTO users (email, password_hash, name, role, org_id, must_change_password, created_at)
                   VALUES (%s, %s, %s, %s, %s, TRUE, NOW())
                   RETURNING id, email, name, role, created_at""",
                (email, password_hash, name or None, role, org_id),
            )
            row = cur.fetchone()
        conn.commit()

        new_user_id = row[0]
        try:
            from utils.auth.enforcer import assign_role_to_user
            if org_id:
                assign_role_to_user(new_user_id, role, org_id)
            else:
                enforcer = get_enforcer()
                enforcer.add_grouping_policy(new_user_id, role, "*")
                enforcer.save_policy()
        except Exception as casbin_err:
            logger.warning("Failed to assign Casbin role for %s: %s", new_user_id, casbin_err)

        logger.info("Admin %s created user %s (%s) with role '%s'", user_id, new_user_id, email, role)
        return jsonify({
            "id": row[0],
            "email": row[1],
            "name": row[2],
            "role": row[3] or "viewer",
            "created_at": row[4].isoformat() if row[4] else None,
        }), 201
    finally:
        conn.close()


@admin_bp.route("/users/<target_user_id>/roles", methods=["GET"])
@require_permission("users", "manage")
def get_user_roles(user_id, target_user_id):
    """Get the roles assigned to a specific user."""
    org_id = get_org_id_from_request()

    # Verify target user belongs to the caller's org
    conn = connect_to_db_as_user()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM users WHERE id = %s AND org_id = %s", (target_user_id, org_id))
            if not cur.fetchone():
                return jsonify({"error": "User not found in this organization"}), 404
    finally:
        conn.close()

    enforcer = get_enforcer()
    if org_id:
        roles = enforcer.get_roles_for_user_in_domain(target_user_id, org_id)
    else:
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
    org_id = get_org_id_from_request()

    conn = connect_to_db_as_user()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM users WHERE id = %s AND org_id = %s", (target_user_id, org_id))
            if not cur.fetchone():
                return jsonify({"error": "Target user not found in this organization"}), 404
    finally:
        conn.close()

    # Remove any existing role assignments for this user
    if org_id:
        current_roles = enforcer.get_roles_for_user_in_domain(target_user_id, org_id)
        for old_role in current_roles:
            enforcer.remove_grouping_policy(target_user_id, old_role, org_id)
        enforcer.add_grouping_policy(target_user_id, role, org_id)
    else:
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
            cur.execute("SET myapp.current_user_id = %s;", (user_id,))
            if org_id:
                cur.execute("SET myapp.current_org_id = %s;", (org_id,))
            conn.commit()
            cur.execute("UPDATE users SET role = %s WHERE id = %s AND org_id = %s", (role, target_user_id, org_id))
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
    org_id = get_org_id_from_request()

    conn = connect_to_db_as_user()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM users WHERE id = %s AND org_id = %s", (target_user_id, org_id))
            if not cur.fetchone():
                return jsonify({"error": "Target user not found in this organization"}), 404
    finally:
        conn.close()

    if org_id:
        enforcer.remove_grouping_policy(target_user_id, role, org_id)
        remaining = enforcer.get_roles_for_user_in_domain(target_user_id, org_id)
        if not remaining:
            enforcer.add_grouping_policy(target_user_id, "viewer", org_id)
    else:
        enforcer.remove_grouping_policy(target_user_id, role)
        remaining = enforcer.get_roles_for_user(target_user_id)
        if not remaining:
            enforcer.add_grouping_policy(target_user_id, "viewer")

    enforcer.save_policy()
    reload_policies()

    if org_id:
        fallback_role = (enforcer.get_roles_for_user_in_domain(target_user_id, org_id) or ["viewer"])[0]
    else:
        fallback_role = (enforcer.get_roles_for_user(target_user_id) or ["viewer"])[0]

    conn = connect_to_db_as_user()
    try:
        with conn.cursor() as cur:
            cur.execute("SET myapp.current_user_id = %s;", (user_id,))
            if org_id:
                cur.execute("SET myapp.current_org_id = %s;", (org_id,))
            conn.commit()
            cur.execute("UPDATE users SET role = %s WHERE id = %s AND org_id = %s", (fallback_role, target_user_id, org_id))
        conn.commit()
    finally:
        conn.close()

    logger.info("Role '%s' revoked from user %s by admin %s (now: %s)",
                role, target_user_id, user_id, fallback_role)
    return jsonify({"user_id": target_user_id, "role": fallback_role}), 200


@admin_bp.route("/users/<target_user_id>", methods=["DELETE"])
@require_permission("users", "manage")
def delete_user(user_id, target_user_id):
    """Permanently delete a user from the organization."""
    if user_id == target_user_id:
        return jsonify({"error": "Cannot delete your own account"}), 400

    org_id = get_org_id_from_request()
    conn = connect_to_db_as_user()
    try:
        with conn.cursor() as cur:
            cur.execute("SET myapp.current_user_id = %s;", (user_id,))
            if org_id:
                cur.execute("SET myapp.current_org_id = %s;", (org_id,))
            conn.commit()

            cur.execute(
                "SELECT id, email FROM users WHERE id = %s AND org_id = %s",
                (target_user_id, org_id),
            )
            target = cur.fetchone()
            if not target:
                return jsonify({"error": "User not found in this organization"}), 404

            target_email = target[1]

            cur.execute(
                "DELETE FROM users WHERE id = %s AND org_id = %s",
                (target_user_id, org_id),
            )
        conn.commit()
    finally:
        conn.close()

    try:
        enforcer = get_enforcer()
        if org_id:
            roles = enforcer.get_roles_for_user_in_domain(target_user_id, org_id)
            for r in roles:
                enforcer.remove_grouping_policy(target_user_id, r, org_id)
        else:
            roles = enforcer.get_roles_for_user(target_user_id)
            for r in roles:
                enforcer.remove_grouping_policy(target_user_id, r)
        enforcer.save_policy()
        reload_policies()
    except Exception as casbin_err:
        logger.warning("Failed to clean up Casbin policies for deleted user %s: %s",
                        target_user_id, casbin_err)

    logger.info("Admin %s deleted user %s (%s)", user_id, target_user_id, target_email)
    return jsonify({"message": "User deleted", "id": target_user_id}), 200
