"""Organization management routes."""

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from flask import Blueprint, request, jsonify
from utils.db.connection_pool import db_pool
from utils.db.org_backfill import backfill_user_org_data, migrate_user_to_org, _USER_SCOPED_TABLES_SQL, _INCIDENT_CHILD_TABLES_SQL
from utils.auth import VALID_ROLES
from utils.auth.rbac_decorators import require_permission, require_auth_only
from utils.auth.stateless_auth import get_org_id_from_request
from utils.auth.enforcer import assign_role_to_user, remove_role_from_user, get_user_roles_in_org

logger = logging.getLogger(__name__)

INVITATION_TTL_DAYS = 7

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


def _purge_vault_secrets(cursor, *, user_id: str = None, org_id: str = None):
    """Delete Vault secrets referenced by user_tokens rows before they are removed.

    Call this BEFORE deleting rows from user_tokens so that the secret_ref
    pointers are still available for lookup.  Failures are logged but do not
    abort the caller — the DB rows will still be cleaned up.
    """
    try:
        if user_id and org_id:
            cursor.execute(
                "SELECT secret_ref FROM user_tokens "
                "WHERE user_id = %s AND org_id = %s AND secret_ref IS NOT NULL",
                (user_id, org_id),
            )
        elif org_id:
            cursor.execute(
                "SELECT secret_ref FROM user_tokens "
                "WHERE org_id = %s AND secret_ref IS NOT NULL",
                (org_id,),
            )
        else:
            return

        refs = [row[0] for row in cursor.fetchall() if row[0]]
        if not refs:
            return

        from utils.secrets.secret_ref_utils import SecretRefManager
        mgr = SecretRefManager()
        for ref in refs:
            try:
                mgr.delete_secret(ref)
                logger.info("Deleted Vault secret: %s", ref)
            except Exception as e:
                logger.warning("Failed to delete Vault secret %s: %s", ref, e)
    except Exception as e:
        logger.warning("Failed to purge Vault secrets: %s", e)


def _cleanup_empty_org(cursor, org_id: str) -> bool:
    """Delete an org and all its scoped data if it has no remaining members."""
    cursor.execute("SELECT COUNT(*) FROM users WHERE org_id = %s", (org_id,))
    if cursor.fetchone()[0] == 0:
        cursor.execute("DELETE FROM org_invitations WHERE org_id = %s", (org_id,))
        _purge_vault_secrets(cursor, org_id=org_id)
        cursor.execute(_USER_SCOPED_TABLES_SQL)
        for (tbl,) in cursor.fetchall():
            try:
                cursor.execute(f'DELETE FROM "{tbl}" WHERE org_id = %s', (org_id,))
            except Exception as e:
                logger.warning("Failed to clean up %s for empty org %s: %s", tbl, org_id, e)
        cursor.execute("DELETE FROM organizations WHERE id = %s", (org_id,))
        logger.info("Cleaned up empty org %s and its data", org_id)
        return True
    return False


def _delete_user_org_data(cursor, user_id: str, org_id: str):
    """Delete a user's connections, tokens, and other org-scoped data from the old org.

    Called when a user joins an existing org so their old data doesn't linger.
    Vault secrets referenced by user_tokens.secret_ref are deleted first to
    avoid orphaned secrets.
    """
    _purge_vault_secrets(cursor, user_id=user_id, org_id=org_id)

    cursor.execute(_USER_SCOPED_TABLES_SQL)
    for (tbl,) in cursor.fetchall():
        try:
            cursor.execute(f'DELETE FROM "{tbl}" WHERE user_id = %s AND org_id = %s', (user_id, org_id))
        except Exception as e:
            logger.warning("Failed to delete %s data for user %s in org %s: %s", tbl, user_id, org_id, e)


_SHARED_ORG_TABLES = frozenset({
    "user_connections", "user_tokens", "user_manual_vms",
})


def _migrate_user_data_only(cursor, user_id: str, new_org_id: str, old_org_id: str):
    """Migrate a user's personal data (incidents, chats) to the new org and
    delete their shared org resources (connections, tokens, VMs) from the old org.

    Used when a user leaves "Default Organization" by accepting an invite.
    Connections/tokens are deleted (not migrated) because the new org has its
    own integrations, and leaving them behind creates ghost references via the
    connector status query's user_id OR org_id filter.
    """
    from utils.db.org_backfill import _safe_update

    cursor.execute(_USER_SCOPED_TABLES_SQL)
    for (tbl,) in cursor.fetchall():
        if tbl in _SHARED_ORG_TABLES:
            try:
                cursor.execute(
                    f'DELETE FROM "{tbl}" WHERE user_id = %s AND org_id = %s',
                    (user_id, old_org_id),
                )
            except Exception as e:
                logger.warning("Failed to delete %s for user %s in org %s: %s", tbl, user_id, old_org_id, e)
            continue
        _safe_update(
            cursor, f"partial_migrate_{tbl}",
            f'UPDATE "{tbl}" SET org_id = %s WHERE user_id = %s',
            (new_org_id, user_id),
        )

    cursor.execute(_INCIDENT_CHILD_TABLES_SQL)
    for (tbl,) in cursor.fetchall():
        _safe_update(
            cursor, f"partial_migrate_child_{tbl}",
            f'UPDATE "{tbl}" c SET org_id = %s '
            f"FROM incidents i WHERE c.incident_id = i.id "
            f"AND i.user_id = %s",
            (new_org_id, user_id),
        )

    logger.info("Migrated user-specific data for %s to org %s (deleted shared resources from old org)", user_id, new_org_id)


def _transfer_user_to_org(cursor, user_id: str, old_org_id, new_org_id: str, new_role: str, is_new_org: bool = False):
    """Move a user between orgs.

    When is_new_org=True (user is creating the org), all their data is migrated.
    When leaving a "Default Organization" to join an *existing* org, only
    user-specific data (incidents, chats) migrates — shared org resources
    (connections, tokens) are deleted from the old org to prevent ghost
    references in the connector status query.
    Otherwise (joining an established org from another established org),
    old connections/tokens are deleted.
    """
    if old_org_id and old_org_id != new_org_id:
        should_migrate = is_new_org
        if not should_migrate:
            cursor.execute(
                "SELECT LOWER(name) FROM organizations WHERE id = %s",
                (old_org_id,),
            )
            row = cursor.fetchone()
            should_migrate = row and row[0] == "default organization"

        if should_migrate and is_new_org:
            migrate_user_to_org(cursor, user_id, new_org_id)
        elif should_migrate:
            _migrate_user_data_only(cursor, user_id, new_org_id, old_org_id)
        else:
            _delete_user_org_data(cursor, user_id, old_org_id)
        cursor.execute(
            "UPDATE users SET org_id = %s, role = %s WHERE id = %s RETURNING id, email, name",
            (new_org_id, new_role, user_id),
        )
        row = cursor.fetchone()
        _cleanup_empty_org(cursor, old_org_id)
        return row
    elif old_org_id == new_org_id:
        cursor.execute(
            "UPDATE users SET role = %s WHERE id = %s RETURNING id, email, name",
            (new_role, user_id),
        )
    else:
        cursor.execute(
            "UPDATE users SET org_id = %s, role = %s WHERE id = %s RETURNING id, email, name",
            (new_org_id, new_role, user_id),
        )
        row = cursor.fetchone()
        backfill_user_org_data(cursor, user_id, new_org_id)
        return row
    return cursor.fetchone()


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
        import re as _re
        _org_name_re = _re.compile(r"^[\w\s\-\.,'&()]+$", _re.UNICODE)
        if not _org_name_re.match(name):
            return jsonify({"error": "Name can only contain letters, numbers, spaces, hyphens, periods, commas, apostrophes, ampersands, and parentheses"}), 400

    if slug:
        slug = slug.strip().lower()
        if not SLUG_REGEX.match(slug):
            return jsonify({"error": "Slug must be 2-50 lowercase alphanumeric characters or hyphens"}), 400

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                if name:
                    cursor.execute(
                        "SELECT id FROM organizations WHERE LOWER(name) = LOWER(%s) AND id != %s",
                        (name, org_id)
                    )
                    if cursor.fetchone():
                        return jsonify({"error": "An organization with this name already exists. Please contact your organization's admin to get an account."}), 409

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
                cursor.execute(
                    "SELECT org_id FROM users WHERE id = %s",
                    (target_user_id,),
                )
                user_row = cursor.fetchone()
                if not user_row:
                    return jsonify({"error": "User not found"}), 404

                old_org_id = user_row[0]
                row = _transfer_user_to_org(cursor, target_user_id, old_org_id, org_id, role)

                if not row:
                    conn.rollback()
                    return jsonify({"error": "User not found"}), 404

                conn.commit()

                if old_org_id and old_org_id != org_id:
                    try:
                        for r in get_user_roles_in_org(target_user_id, old_org_id):
                            remove_role_from_user(target_user_id, r, old_org_id)
                    except Exception as e:
                        logger.error("Failed to remove old Casbin roles: %s", e)

                try:
                    assign_role_to_user(target_user_id, role, org_id)
                except Exception as e:
                    logger.error("Failed to assign Casbin role: %s", e)

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

                for r in get_user_roles_in_org(target_user_id, org_id):
                    remove_role_from_user(target_user_id, r, org_id)

                return jsonify({"removed": True})
    except Exception as e:
        logger.error("Error removing member: %s", e)
        return jsonify({"error": "Failed to remove member"}), 500


@org_bp.route("/my-invitations", methods=["GET", "OPTIONS"])
@require_auth_only
def my_invitations(user_id):
    """Return pending invitations addressed to the current user's email."""
    if request.method == "OPTIONS":
        return jsonify({}), 200

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT email FROM users WHERE id = %s", (user_id,))
                user_row = cursor.fetchone()
                if not user_row:
                    return jsonify({"invitations": []}), 200

                user_email = user_row[0].lower()

                cursor.execute(
                    """UPDATE org_invitations SET status = 'expired'
                       WHERE LOWER(email) = %s AND status = 'pending'
                         AND expires_at IS NOT NULL AND expires_at <= NOW()""",
                    (user_email,),
                )
                conn.commit()

                cursor.execute(
                    """SELECT i.id, o.name, i.role, i.created_at, i.expires_at,
                              u.name AS invited_by_name, u.email AS invited_by_email
                       FROM org_invitations i
                       JOIN organizations o ON i.org_id = o.id
                       LEFT JOIN users u ON i.invited_by = u.id
                       WHERE LOWER(i.email) = %s AND i.status = 'pending'
                         AND (i.expires_at IS NULL OR i.expires_at > NOW())
                       ORDER BY i.created_at DESC""",
                    (user_email,),
                )
                invitations = [
                    {
                        "id": row[0],
                        "orgName": row[1],
                        "role": row[2],
                        "createdAt": row[3].isoformat() if row[3] else None,
                        "expiresAt": row[4].isoformat() if row[4] else None,
                        "invitedBy": row[5] or row[6],
                    }
                    for row in cursor.fetchall()
                ]
                return jsonify({"invitations": invitations}), 200
    except Exception as e:
        logger.error("Error fetching user invitations: %s", e)
        return jsonify({"error": "Failed to fetch invitations"}), 500


@org_bp.route("/my-invitations/<invitation_id>/decline", methods=["POST", "OPTIONS"])
@require_auth_only
def decline_invitation(user_id, invitation_id):
    """Decline a pending invitation addressed to the current user."""
    if request.method == "OPTIONS":
        return jsonify({}), 200

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT email FROM users WHERE id = %s", (user_id,))
                user_row = cursor.fetchone()
                if not user_row:
                    return jsonify({"error": "User not found"}), 404

                cursor.execute(
                    """UPDATE org_invitations SET status = 'declined'
                       WHERE id = %s AND LOWER(email) = LOWER(%s) AND status = 'pending'
                       RETURNING id""",
                    (invitation_id, user_row[0]),
                )
                row = cursor.fetchone()
                conn.commit()

                if not row:
                    return jsonify({"error": "Invitation not found or already handled"}), 404

                return jsonify({"declined": True}), 200
    except Exception as e:
        logger.error("Error declining invitation: %s", e)
        return jsonify({"error": "Failed to decline invitation"}), 500


@org_bp.route("/invitations/<invitation_id>/cancel", methods=["POST", "OPTIONS"])
@require_permission("users", "manage")
def cancel_invitation(user_id, invitation_id):
    """Cancel a pending invitation (admin only)."""
    if request.method == "OPTIONS":
        return jsonify({}), 200

    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No organization found"}), 404

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """UPDATE org_invitations SET status = 'cancelled'
                       WHERE id = %s AND org_id = %s AND status = 'pending'
                       RETURNING id""",
                    (invitation_id, org_id),
                )
                row = cursor.fetchone()
                conn.commit()

                if not row:
                    return jsonify({"error": "Invitation not found or already handled"}), 404

                return jsonify({"cancelled": True}), 200
    except Exception as e:
        logger.error("Error cancelling invitation: %s", e)
        return jsonify({"error": "Failed to cancel invitation"}), 500


@org_bp.route("/invitations", methods=["GET", "POST", "OPTIONS"])
@require_permission("users", "manage")
def invitations(user_id):
    """Create or list invitations for this org (admin only)."""
    if request.method == "OPTIONS":
        return jsonify({}), 200

    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No organization found"}), 404

    if request.method == "GET":
        return _list_invitations(org_id)

    return _create_invitation(org_id, user_id)


def _list_invitations(org_id: str):
    """List pending invitations for this org."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                # Mark any past-due invitations so the status column stays accurate
                cursor.execute(
                    """UPDATE org_invitations SET status = 'expired'
                       WHERE org_id = %s AND status = 'pending'
                         AND expires_at IS NOT NULL AND expires_at <= NOW()""",
                    (org_id,),
                )
                conn.commit()

                cursor.execute(
                    """SELECT i.id, i.email, i.role, i.status, i.created_at, i.expires_at,
                              u.name AS invited_by_name, u.email AS invited_by_email,
                              target.name AS target_name
                       FROM org_invitations i
                       LEFT JOIN users u ON i.invited_by = u.id
                       LEFT JOIN users target ON LOWER(target.email) = LOWER(i.email)
                       WHERE i.org_id = %s AND i.status = 'pending'
                         AND (i.expires_at IS NULL OR i.expires_at > NOW())
                       ORDER BY i.created_at DESC""",
                    (org_id,),
                )
                invitations = [
                    {
                        "id": row[0],
                        "email": row[1],
                        "name": row[8],
                        "role": row[2],
                        "status": row[3],
                        "invited_at": row[4].isoformat() if row[4] else None,
                        "expires_at": row[5].isoformat() if row[5] else None,
                        "invitedBy": row[6] or row[7],
                    }
                    for row in cursor.fetchall()
                ]
                return jsonify({"invitations": invitations})
    except Exception as e:
        logger.error("Error listing invitations: %s", e)
        return jsonify({"error": "Failed to list invitations"}), 500


def _create_invitation(org_id: str, user_id: str):
    """Create a new invitation."""
    data = request.get_json() or {}
    email = (data.get("email") or "").strip().lower()
    role = data.get("role", "viewer")

    if not email or not EMAIL_REGEX.match(email):
        return jsonify({"error": "A valid email is required"}), 400

    if role not in VALID_ROLES:
        return jsonify({"error": "Invalid role"}), 400

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                # Expire stale invitations before checking for duplicates
                cursor.execute(
                    """UPDATE org_invitations SET status = 'expired'
                       WHERE org_id = %s AND email = %s AND status = 'pending'
                         AND expires_at IS NOT NULL AND expires_at <= NOW()""",
                    (org_id, email),
                )
                cursor.execute(
                    "SELECT id FROM org_invitations WHERE org_id = %s AND email = %s AND status = 'pending'",
                    (org_id, email),
                )
                if cursor.fetchone():
                    return jsonify({"error": "An invitation for this email already exists"}), 409

                cursor.execute(
                    """DELETE FROM org_invitations
                       WHERE org_id = %s AND email = %s AND status IN ('cancelled', 'declined', 'expired')""",
                    (org_id, email),
                )

                invitation_id = str(uuid.uuid4())
                expires_at = datetime.now(timezone.utc) + timedelta(days=INVITATION_TTL_DAYS)

                cursor.execute(
                    """INSERT INTO org_invitations (id, org_id, email, role, invited_by, status, expires_at)
                       VALUES (%s, %s, %s, %s, %s, 'pending', %s)
                       RETURNING id, org_id, email, role, status, created_at, expires_at""",
                    (invitation_id, org_id, email, role, user_id, expires_at),
                )
                row = cursor.fetchone()
                conn.commit()

                return jsonify({
                    "id": row[0],
                    "orgId": row[1],
                    "email": row[2],
                    "role": row[3],
                    "status": row[4],
                    "createdAt": row[5].isoformat() if row[5] else None,
                    "expiresAt": row[6].isoformat() if row[6] else None,
                }), 201
    except Exception as e:
        logger.error("Error creating invitation: %s", e)
        return jsonify({"error": "Failed to create invitation"}), 500


@org_bp.route("/join", methods=["POST", "OPTIONS"])
@require_auth_only
def join_org(user_id):
    """Accept an invitation and transfer user data to the new org."""
    if request.method == "OPTIONS":
        return jsonify({}), 200

    data = request.get_json() or {}
    invitation_id = data.get("invitation_id")
    direct_org_id = data.get("org_id")

    if not invitation_id and not direct_org_id:
        return jsonify({"error": "invitation_id or org_id is required"}), 400

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                new_org_id = None
                new_role = "viewer"

                if invitation_id:
                    cursor.execute(
                        """SELECT i.org_id, i.email, i.role, i.status, i.expires_at
                           FROM org_invitations i WHERE i.id = %s
                           FOR UPDATE""",
                        (invitation_id,),
                    )
                    inv = cursor.fetchone()
                    if not inv:
                        return jsonify({"error": "Invitation not found"}), 404

                    inv_org_id, inv_email, inv_role, inv_status, inv_expires = inv

                    if inv_status != "pending":
                        return jsonify({"error": f"Invitation is already {inv_status}"}), 400

                    if inv_expires and inv_expires.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
                        cursor.execute(
                            "UPDATE org_invitations SET status = 'expired' WHERE id = %s",
                            (invitation_id,),
                        )
                        conn.commit()
                        return jsonify({"error": "Invitation has expired"}), 410

                    cursor.execute("SELECT email FROM users WHERE id = %s", (user_id,))
                    user_email_row = cursor.fetchone()
                    if not user_email_row or user_email_row[0].lower() != inv_email.lower():
                        return jsonify({"error": "Invitation email does not match your account"}), 403

                    new_org_id = inv_org_id
                    new_role = inv_role or "viewer"
                else:
                    # Direct org_id join requires a pending invitation matching this user's email
                    cursor.execute("SELECT email FROM users WHERE id = %s", (user_id,))
                    email_row = cursor.fetchone()
                    if not email_row:
                        return jsonify({"error": "User not found"}), 404
                    cursor.execute(
                        """SELECT id, role FROM org_invitations
                           WHERE org_id = %s AND LOWER(email) = LOWER(%s) AND status = 'pending'
                           AND (expires_at IS NULL OR expires_at > NOW())
                           LIMIT 1
                           FOR UPDATE""",
                        (direct_org_id, email_row[0]),
                    )
                    matching_inv = cursor.fetchone()
                    if not matching_inv:
                        return jsonify({"error": "No valid invitation found for this organization"}), 403
                    invitation_id = matching_inv[0]
                    new_org_id = direct_org_id
                    new_role = matching_inv[1] or "viewer"

                cursor.execute("SELECT org_id FROM users WHERE id = %s FOR UPDATE", (user_id,))
                current = cursor.fetchone()
                if not current:
                    return jsonify({"error": "User not found"}), 404

                old_org_id = current[0]

                if old_org_id == new_org_id:
                    if invitation_id:
                        cursor.execute(
                            "UPDATE org_invitations SET status = 'accepted' WHERE id = %s",
                            (invitation_id,),
                        )
                        conn.commit()
                    return jsonify({"error": "You are already a member of this organization"}), 409

                _transfer_user_to_org(cursor, user_id, old_org_id, new_org_id, new_role)

                if invitation_id:
                    cursor.execute(
                        "UPDATE org_invitations SET status = 'accepted' WHERE id = %s",
                        (invitation_id,),
                    )

                cursor.execute(
                    "SELECT id, name, slug FROM organizations WHERE id = %s",
                    (new_org_id,),
                )
                org_row = cursor.fetchone()

                conn.commit()

                # Casbin updates after commit so DB state is consistent on failure
                if old_org_id:
                    try:
                        for r in get_user_roles_in_org(user_id, old_org_id):
                            remove_role_from_user(user_id, r, old_org_id)
                    except Exception as e:
                        logger.error("Failed to remove old Casbin roles for %s: %s", user_id, e)

                try:
                    assign_role_to_user(user_id, new_role, new_org_id)
                except Exception as e:
                    logger.error("Failed to assign Casbin role for %s in org %s: %s", user_id, new_org_id, e)

                return jsonify({
                    "id": org_row[0],
                    "name": org_row[1],
                    "slug": org_row[2],
                    "role": new_role,
                })
    except Exception as e:
        logger.error("Error joining org: %s", e)
        return jsonify({"error": "Failed to join organization"}), 500


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
                    """SELECT i.id, i.source_type, i.alert_title, i.severity,
                              i.status, i.created_at
                       FROM incidents i
                       WHERE i.org_id = %s
                       ORDER BY i.created_at DESC LIMIT %s""",
                    (org_id, limit),
                )
                for row in cursor.fetchall():
                    ts = row[5]
                    events.append({
                        "type": "incident_created",
                        "incidentId": row[0],
                        "source": row[1],
                        "title": row[2],
                        "severity": row[3],
                        "status": row[4],
                        "timestamp": ts.isoformat() if ts else None,
                        "description": row[2] or f"Incident from {row[1]}",
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

                cursor.execute(
                    """SELECT uc.provider, uc.last_verified_at, u.name, u.email
                       FROM user_connections uc
                       JOIN users u ON uc.user_id = u.id
                       WHERE (uc.org_id = %s OR u.org_id = %s)
                         AND uc.status = 'active'
                       ORDER BY uc.last_verified_at DESC LIMIT %s""",
                    (org_id, org_id, limit),
                )
                for row in cursor.fetchall():
                    ts = row[1]
                    who = row[2] or row[3]
                    provider = row[0]
                    if not any(
                        e.get("type") == "connector_added" and e.get("provider") == provider
                        for e in events
                    ):
                        events.append({
                            "type": "connector_added",
                            "provider": provider,
                            "timestamp": ts.isoformat() if ts else None,
                            "description": f"{who} connected {provider}",
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
