"""Organization management routes."""

import logging
from flask import Blueprint, request, jsonify
from utils.db.connection_pool import db_pool
from utils.auth.rbac_decorators import require_permission, require_auth_only
from utils.auth.stateless_auth import get_org_id_from_request

logger = logging.getLogger(__name__)

org_bp = Blueprint("org", __name__, url_prefix="/api/orgs")


@org_bp.route("/current", methods=["GET", "OPTIONS"])
@require_auth_only
def get_current_org(user_id):
    """Get the current user's organization details and member list."""
    if request.method == "OPTIONS":
        return jsonify({}), 200

    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No organization found"}), 404

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT id, name, slug, created_by, created_at FROM organizations WHERE id = %s",
                    (org_id,),
                )
                org = cursor.fetchone()
                if not org:
                    return jsonify({"error": "Organization not found"}), 404

                cursor.execute(
                    "SELECT id, email, name, role, created_at FROM users WHERE org_id = %s ORDER BY created_at",
                    (org_id,),
                )
                members = [
                    {
                        "id": row[0],
                        "email": row[1],
                        "name": row[2],
                        "role": row[3] or "viewer",
                        "createdAt": row[4].isoformat() if row[4] else None,
                    }
                    for row in cursor.fetchall()
                ]

                return jsonify({
                    "id": org[0],
                    "name": org[1],
                    "slug": org[2],
                    "createdBy": org[3],
                    "createdAt": org[4].isoformat() if org[4] else None,
                    "members": members,
                })
    except Exception as e:
        logger.error("Error fetching org: %s", e)
        return jsonify({"error": "Failed to fetch organization"}), 500


@org_bp.route("", methods=["PATCH", "OPTIONS"])
@require_permission("org", "manage")
def update_org(user_id):
    """Update organization name or slug (admin only)."""
    if request.method == "OPTIONS":
        return jsonify({}), 200

    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No organization found"}), 404

    data = request.get_json() or {}
    name = data.get("name")
    slug = data.get("slug")

    if not name and not slug:
        return jsonify({"error": "name or slug required"}), 400

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                updates = []
                params = []
                if name:
                    updates.append("name = %s")
                    params.append(name)
                if slug:
                    updates.append("slug = %s")
                    params.append(slug)
                updates.append("updated_at = NOW()")
                params.append(org_id)

                cursor.execute(
                    f"UPDATE organizations SET {', '.join(updates)} WHERE id = %s RETURNING id, name, slug",
                    tuple(params),
                )
                row = cursor.fetchone()
                conn.commit()

                if not row:
                    return jsonify({"error": "Organization not found"}), 404

                return jsonify({"id": row[0], "name": row[1], "slug": row[2]})
    except Exception as e:
        logger.error("Error updating org: %s", e)
        return jsonify({"error": "Failed to update organization"}), 500


@org_bp.route("/members", methods=["POST", "OPTIONS"])
@require_permission("users", "manage")
def add_member(user_id):
    """Add an existing user to this org with a role (admin only)."""
    if request.method == "OPTIONS":
        return jsonify({}), 200

    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No organization found"}), 404

    data = request.get_json() or {}
    target_user_id = data.get("userId")
    role = data.get("role", "viewer")

    if not target_user_id:
        return jsonify({"error": "userId is required"}), 400

    if role not in ("admin", "editor", "viewer"):
        return jsonify({"error": "Invalid role"}), 400

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE users SET org_id = %s, role = %s WHERE id = %s RETURNING id, email, name",
                    (org_id, role, target_user_id),
                )
                row = cursor.fetchone()
                conn.commit()

                if not row:
                    return jsonify({"error": "User not found"}), 404

                from utils.auth.enforcer import assign_role_to_user
                assign_role_to_user(target_user_id, role, org_id)

                return jsonify({
                    "id": row[0],
                    "email": row[1],
                    "name": row[2],
                    "role": role,
                })
    except Exception as e:
        logger.error("Error adding member: %s", e)
        return jsonify({"error": "Failed to add member"}), 500


@org_bp.route("/members/<target_user_id>", methods=["DELETE", "OPTIONS"])
@require_permission("users", "manage")
def remove_member(user_id, target_user_id):
    """Remove a user from this org (admin only)."""
    if request.method == "OPTIONS":
        return jsonify({}), 200

    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No organization found"}), 404

    if target_user_id == user_id:
        return jsonify({"error": "Cannot remove yourself"}), 400

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE users SET org_id = NULL WHERE id = %s AND org_id = %s RETURNING id",
                    (target_user_id, org_id),
                )
                row = cursor.fetchone()
                conn.commit()

                if not row:
                    return jsonify({"error": "User not found in this org"}), 404

                from utils.auth.enforcer import remove_role_from_user, get_user_roles_in_org
                for r in get_user_roles_in_org(target_user_id, org_id):
                    remove_role_from_user(target_user_id, r, org_id)

                return jsonify({"removed": True})
    except Exception as e:
        logger.error("Error removing member: %s", e)
        return jsonify({"error": "Failed to remove member"}), 500
