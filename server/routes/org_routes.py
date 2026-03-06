"""Organization management routes."""

import logging
import re
from flask import Blueprint, request, jsonify
from utils.db.connection_pool import db_pool
from utils.auth import VALID_ROLES
from utils.auth.rbac_decorators import require_permission, require_auth_only
from utils.auth.stateless_auth import get_org_id_from_request

logger = logging.getLogger(__name__)

org_bp = Blueprint("org", __name__, url_prefix="/api/orgs")

EMAIL_REGEX = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')
SLUG_REGEX = re.compile(r'^[a-z0-9][a-z0-9-]{0,48}[a-z0-9]$')


def _validate_org_id_for_user(user_id: str, org_id: str) -> bool:
    """Check that the user actually belongs to the claimed org."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT 1 FROM users WHERE id = %s AND org_id = %s",
                    (user_id, org_id),
                )
                return cursor.fetchone() is not None
    except Exception:
        return False


@org_bp.route("/current", methods=["GET", "OPTIONS"])
@require_auth_only
def get_current_org(user_id):
    """Get the current user's organization details and member list."""
    if request.method == "OPTIONS":
        return jsonify({}), 200

    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No organization found"}), 404

    if not _validate_org_id_for_user(user_id, org_id):
        return jsonify({"error": "Forbidden"}), 403

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

    if name:
        name = name.strip()
        if not name or len(name) > 100:
            return jsonify({"error": "Name must be 1-100 characters"}), 400

    if slug:
        slug = slug.strip().lower()
        if not SLUG_REGEX.match(slug):
            return jsonify({"error": "Slug must be 2-50 lowercase alphanumeric characters or hyphens"}), 400

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

    if role not in VALID_ROLES:
        return jsonify({"error": "Invalid role"}), 400

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                # Check if user is already in a different org
                cursor.execute(
                    "SELECT org_id FROM users WHERE id = %s",
                    (target_user_id,),
                )
                user_row = cursor.fetchone()
                if not user_row:
                    return jsonify({"error": "User not found"}), 404
                if user_row[0] and user_row[0] != org_id:
                    return jsonify({"error": "User already belongs to another organization"}), 409

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
                # Ensure at least one admin remains after removal
                cursor.execute(
                    "SELECT COUNT(*) FROM users WHERE org_id = %s AND role = 'admin' AND id != %s",
                    (org_id, target_user_id),
                )
                remaining_admins = cursor.fetchone()[0]
                cursor.execute(
                    "SELECT role FROM users WHERE id = %s AND org_id = %s",
                    (target_user_id, org_id),
                )
                target_row = cursor.fetchone()
                if target_row and target_row[0] == 'admin' and remaining_admins < 1:
                    return jsonify({"error": "Cannot remove the last admin"}), 400

                # Clear FK references before deleting the user
                cursor.execute(
                    "DELETE FROM org_invitations WHERE invited_by = %s", (target_user_id,)
                )
                cursor.execute(
                    "UPDATE organizations SET created_by = NULL WHERE created_by = %s", (target_user_id,)
                )
                # Clean up user-scoped data
                for tbl in (
                    "user_tokens", "user_connections", "user_manual_vms",
                    "user_preferences", "rca_notification_emails",
                ):
                    cursor.execute(f"DELETE FROM {tbl} WHERE user_id = %s", (target_user_id,))

                cursor.execute(
                    "DELETE FROM users WHERE id = %s AND org_id = %s RETURNING id",
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


@org_bp.route("/stats", methods=["GET", "OPTIONS"])
@require_auth_only
def get_org_stats(user_id):
    """Return aggregate stats for the current org."""
    if request.method == "OPTIONS":
        return jsonify({}), 200

    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No organization found"}), 404

    if not _validate_org_id_for_user(user_id, org_id):
        return jsonify({"error": "Forbidden"}), 403

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT COUNT(*) FROM users WHERE org_id = %s", (org_id,)
                )
                member_count = cursor.fetchone()[0]

                cursor.execute(
                    "SELECT COUNT(*) FROM incidents WHERE org_id = %s", (org_id,)
                )
                incident_count = cursor.fetchone()[0]

                cursor.execute(
                    "SELECT COUNT(*) FROM chat_sessions WHERE org_id = %s",
                    (org_id,),
                )
                chat_count = cursor.fetchone()[0]

                from routes.connector_status import get_connected_count
                integration_count = get_connected_count(user_id, org_id)

                return jsonify({
                    "members": member_count,
                    "incidents": incident_count,
                    "chatSessions": chat_count,
                    "integrations": integration_count,
                })
    except Exception as e:
        logger.error("Error fetching org stats: %s", e)
        return jsonify({"error": "Failed to fetch stats"}), 500


@org_bp.route("/activity", methods=["GET", "OPTIONS"])
@require_auth_only
def get_org_activity(user_id):
    """Return recent activity events for the org (member joins, role changes)."""
    if request.method == "OPTIONS":
        return jsonify({}), 200

    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No organization found"}), 404

    limit = request.args.get("limit", 30, type=int)
    limit = max(1, min(limit, 200))

    try:
        events = []
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """SELECT id, email, name, role, created_at
                       FROM users WHERE org_id = %s
                       ORDER BY created_at DESC LIMIT %s""",
                    (org_id, limit),
                )
                for row in cursor.fetchall():
                    ts = row[4]
                    events.append({
                        "type": "member_joined",
                        "userId": row[0],
                        "email": row[1],
                        "name": row[2],
                        "role": row[3] or "viewer",
                        "timestamp": ts.isoformat() if ts else None,
                        "description": f"{row[2] or row[1]} joined as {row[3] or 'viewer'}",
                    })

                cursor.execute(
                    """SELECT i.source_type, i.alert_title, i.severity,
                              i.status, i.created_at
                       FROM incidents i
                       WHERE i.org_id = %s
                       ORDER BY i.created_at DESC LIMIT %s""",
                    (org_id, limit),
                )
                for row in cursor.fetchall():
                    ts = row[4]
                    events.append({
                        "type": "incident_created",
                        "source": row[0],
                        "title": row[1],
                        "severity": row[2],
                        "status": row[3],
                        "timestamp": ts.isoformat() if ts else None,
                        "description": f"Incident from {row[0]}: {row[1]}",
                    })

                cursor.execute(
                    """SELECT ut.provider, ut.timestamp, u.name, u.email
                       FROM user_tokens ut
                       JOIN users u ON ut.user_id = u.id
                       WHERE (ut.org_id = %s OR u.org_id = %s)
                         AND ut.secret_ref IS NOT NULL AND ut.is_active = TRUE
                       ORDER BY ut.timestamp DESC LIMIT %s""",
                    (org_id, org_id, limit),
                )
                for row in cursor.fetchall():
                    ts = row[1]
                    who = row[2] or row[3]
                    events.append({
                        "type": "connector_added",
                        "provider": row[0],
                        "timestamp": ts.isoformat() if ts else None,
                        "description": f"{who} connected {row[0]}",
                    })

        events.sort(
            key=lambda e: e.get("timestamp") or "",
            reverse=True,
        )
        return jsonify({"events": events[:limit]})
    except Exception as e:
        logger.error("Error fetching org activity: %s", e)
        return jsonify({"error": "Failed to fetch activity"}), 500


@org_bp.route("/preferences", methods=["GET", "OPTIONS"])
@require_auth_only
def get_org_preferences(user_id):
    """Get org-level preferences stored in user_preferences with user_id='__org__'."""
    if request.method == "OPTIONS":
        return jsonify({}), 200

    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No organization found"}), 404

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """SELECT preference_key, preference_value
                       FROM user_preferences
                       WHERE user_id = '__org__' AND org_id = %s""",
                    (org_id,),
                )
                prefs = {row[0]: row[1] for row in cursor.fetchall()}

                cursor.execute(
                    "SELECT email FROM rca_notification_emails WHERE org_id = %s ORDER BY email",
                    (org_id,),
                )
                prefs["notification_emails"] = [r[0] for r in cursor.fetchall()]

                return jsonify(prefs)
    except Exception as e:
        logger.error("Error fetching org preferences: %s", e)
        return jsonify({"error": "Failed to fetch preferences"}), 500


@org_bp.route("/preferences", methods=["PUT", "OPTIONS"])
@require_permission("org", "manage")
def update_org_preferences(user_id):
    """Update org-level preferences (admin only)."""
    if request.method == "OPTIONS":
        return jsonify({}), 200

    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No organization found"}), 404

    data = request.get_json() or {}

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                for key, value in data.items():
                    if key == "notification_emails":
                        continue
                    cursor.execute(
                        """INSERT INTO user_preferences (user_id, org_id, preference_key, preference_value)
                           VALUES ('__org__', %s, %s, %s)
                           ON CONFLICT (user_id, org_id, preference_key)
                           DO UPDATE SET preference_value = EXCLUDED.preference_value""",
                        (org_id, key, str(value)),
                    )

                if "notification_emails" in data:
                    emails = data["notification_emails"]
                    if not isinstance(emails, list) or not all(isinstance(e, str) for e in emails):
                        return jsonify({"error": "notification_emails must be a list of strings"}), 400
                    # Upsert instead of delete-all to preserve is_verified status
                    valid_emails = []
                    for email in emails:
                        email = email.strip()
                        if email and EMAIL_REGEX.match(email):
                            valid_emails.append(email)
                    # Remove emails no longer in the list
                    if valid_emails:
                        cursor.execute(
                            "DELETE FROM rca_notification_emails WHERE org_id = %s AND email NOT IN %s",
                            (org_id, tuple(valid_emails)),
                        )
                    else:
                        cursor.execute(
                            "DELETE FROM rca_notification_emails WHERE org_id = %s",
                            (org_id,),
                        )
                    # Insert new emails (existing ones preserved via ON CONFLICT)
                    for email in valid_emails:
                        cursor.execute(
                            """INSERT INTO rca_notification_emails
                               (user_id, org_id, email) VALUES (%s, %s, %s)
                               ON CONFLICT (org_id, email) DO NOTHING""",
                            (user_id, org_id, email),
                        )
                conn.commit()
                return jsonify({"ok": True})
    except Exception as e:
        logger.error("Error updating org preferences: %s", e)
        return jsonify({"error": "Failed to update preferences"}), 500
