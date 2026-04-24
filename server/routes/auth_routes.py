"""
Auth routes for user registration, login, and password management.
Replaces the previous authentication system.
"""
import logging
from routes.audit_routes import record_audit_event
import re
import bcrypt
from flask import Blueprint, request, jsonify
from utils.db.db_utils import connect_to_db_as_user
from utils.db.connection_pool import db_pool
from utils.auth.rbac_decorators import require_auth_only
import os

auth_bp = Blueprint('auth', __name__, url_prefix='/api/auth')

_DUMMY_BCRYPT_HASH = bcrypt.hashpw(os.urandom(16), bcrypt.gensalt()).decode('utf-8')

FRONTEND_URL = os.getenv("FRONTEND_URL")

SLUG_REGEX = re.compile(r'^[a-z0-9][a-z0-9-]{0,48}[a-z0-9]$')
ORG_NAME_REGEX = re.compile(r"^[\w\s\-\.,'&()]+$", re.UNICODE)
ORG_NAME_ERROR = "Organization name can only contain letters, numbers, spaces, hyphens, periods, commas, apostrophes, ampersands, and parentheses"

def _name_to_slug(name: str) -> str:
    slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:50]
    if len(slug) < 2:
        slug = slug + '-org'
    return slug

@auth_bp.after_request
def add_cors_headers(response):
    """Add CORS headers to all responses from auth routes."""
    origin = request.headers.get('Origin', FRONTEND_URL)
    response.headers['Access-Control-Allow-Origin'] = origin
    response.headers['Access-Control-Allow-Credentials'] = 'true'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-Provider, X-Requested-With, X-User-ID, Authorization'
    return response

@auth_bp.route('/register', methods=['POST'])
def register():
    """Register a new organization with its first admin user.

    Body: { email, password, name, org_name }
    - Creates a new org and assigns the caller as its admin.
    - Users within an existing org are created by an admin via
      /api/admin/users (invite-only).
    """
    
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Invalid request body"}), 400
        
        email = data.get('email')
        password = data.get('password')
        name = data.get('name')
        org_name = (data.get('org_name') or '').strip()
        
        if not email or not password:
            return jsonify({"error": "Email and password are required"}), 400
            
        if len(password) < 8:
            return jsonify({"error": "Password must be at least 8 characters"}), 400

        if not org_name:
            return jsonify({"error": "Organization name is required"}), 400

        if len(org_name) > 100:
            return jsonify({"error": "Organization name must be 100 characters or less"}), 400

        if not ORG_NAME_REGEX.match(org_name):
            return jsonify({"error": ORG_NAME_ERROR}), 400

        password_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
        
        conn = connect_to_db_as_user()
        try:
            with conn.cursor() as cursor:
                # No RLS needed — users, organizations not RLS-protected
                cursor.execute(
                    "SELECT id FROM users WHERE email = %s",
                    (email,)
                )
                if cursor.fetchone():
                    return jsonify({"error": "User with this email already exists"}), 409

                slug = _name_to_slug(org_name)
                cursor.execute(
                    "SELECT id FROM organizations WHERE LOWER(name) = LOWER(%s)",
                    (org_name,)
                )
                if cursor.fetchone():
                    return jsonify({"error": "An organization with this name already exists. Please contact your organization's admin to get an account.", "code": "duplicate_name"}), 409

                cursor.execute(
                    "SELECT id FROM organizations WHERE slug = %s",
                    (slug,)
                )
                if cursor.fetchone():
                    import uuid
                    slug = slug[:42] + '-' + uuid.uuid4().hex[:6]

                cursor.execute(
                    """
                    INSERT INTO users (email, password_hash, name, role, created_at)
                    VALUES (%s, %s, %s, 'admin', NOW())
                    RETURNING id, email, name
                    """,
                    (email, password_hash.decode('utf-8'), name)
                )
                user = cursor.fetchone()
                user_id, user_email, user_name = user[0], user[1], user[2]

                cursor.execute(
                    """
                    INSERT INTO organizations (id, name, slug, created_by)
                    VALUES (gen_random_uuid()::TEXT, %s, %s, %s)
                    RETURNING id, name
                    """,
                    (org_name, slug, user_id)
                )
                org_row = cursor.fetchone()
                org_id, org_display_name = org_row[0], org_row[1]

                cursor.execute(
                    "UPDATE users SET org_id = %s WHERE id = %s",
                    (org_id, user_id)
                )

                conn.commit()

                # Register the user-role mapping in Casbin (domain-aware)
                try:
                    from utils.auth.enforcer import assign_role_to_user
                    assign_role_to_user(user_id, "admin", org_id)
                except Exception as casbin_err:
                    logging.warning(f"Failed to assign Casbin role for {user_id}: {casbin_err}")
                
                logging.info(f"New user registered: {email[:3]}***@*** (role=admin, org={org_id})")

                record_audit_event(org_id, user_id, "register", "organization", org_id,
                                   {"email": email}, request)

                return jsonify({
                    "id": user_id,
                    "email": user_email,
                    "name": user_name,
                    "role": "admin",
                    "orgId": org_id,
                    "orgName": org_display_name,
                }), 201
        finally:
            conn.close()
            
    except Exception as e:
        logging.error(f"Error during registration: {e}")
        return jsonify({"error": "Registration failed"}), 500


@auth_bp.route('/setup-org', methods=['POST'])
@require_auth_only
def setup_org(user_id):
    """Create an organization for an authenticated user who doesn't have one.

    Body: { org_name }
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Invalid request body"}), 400

        org_name = (data.get('org_name') or '').strip()

        if not org_name:
            return jsonify({"error": "Organization name is required"}), 400

        if len(org_name) > 100:
            return jsonify({"error": "Organization name must be 100 characters or less"}), 400

        if not ORG_NAME_REGEX.match(org_name):
            return jsonify({"error": ORG_NAME_ERROR}), 400

        conn = connect_to_db_as_user()
        try:
            with conn.cursor() as cursor:
                # No RLS needed — users, organizations not RLS-protected
                cursor.execute(
                    "SELECT u.id, u.org_id, o.name "
                    "FROM users u LEFT JOIN organizations o ON u.org_id = o.id "
                    "WHERE u.id = %s",
                    (user_id,)
                )
                user_row = cursor.fetchone()
                if not user_row:
                    return jsonify({"error": "User not found"}), 404

                existing_org_id = user_row[1]
                existing_org_name = user_row[2]
                is_default_org = existing_org_name and existing_org_name.lower() == "default organization"

                if existing_org_id and not is_default_org:
                    return jsonify({"error": "You already belong to an organization", "code": "already_has_org"}), 409

                slug = _name_to_slug(org_name)
                cursor.execute(
                    "SELECT id FROM organizations WHERE LOWER(name) = LOWER(%s)",
                    (org_name,)
                )
                if cursor.fetchone():
                    return jsonify({"error": "An organization with this name already exists. Please contact your organization's admin to get an account.", "code": "duplicate_name"}), 409

                cursor.execute(
                    "SELECT id FROM organizations WHERE slug = %s",
                    (slug,)
                )
                if cursor.fetchone():
                    import uuid
                    slug = slug[:42] + '-' + uuid.uuid4().hex[:6]

                cursor.execute(
                    """
                    INSERT INTO organizations (id, name, slug, created_by)
                    VALUES (gen_random_uuid()::TEXT, %s, %s, %s)
                    RETURNING id, name
                    """,
                    (org_name, slug, user_id)
                )
                org_row = cursor.fetchone()
                org_id, org_display_name = org_row[0], org_row[1]

                cursor.execute(
                    "UPDATE users SET org_id = %s, role = 'admin' WHERE id = %s",
                    (org_id, user_id)
                )

                from utils.db.org_backfill import backfill_user_org_data, migrate_user_to_org
                if existing_org_id:
                    migrate_user_to_org(cursor, user_id, org_id)
                    from routes.org_routes import _cleanup_empty_org
                    _cleanup_empty_org(cursor, existing_org_id)
                else:
                    backfill_user_org_data(cursor, user_id, org_id)

                conn.commit()

                try:
                    from utils.auth.enforcer import assign_role_to_user
                    assign_role_to_user(user_id, "admin", org_id)
                except Exception as casbin_err:
                    logging.warning(f"Failed to assign Casbin role for {user_id}: {casbin_err}")

                logging.info(f"User {user_id} created org {org_id} ({org_name})")

                record_audit_event(org_id, user_id, "setup_org", "organization", org_id,
                                   {"org_name": org_name}, request)

                return jsonify({
                    "orgId": org_id,
                    "orgName": org_display_name,
                }), 201
        finally:
            conn.close()

    except Exception as e:
        logging.error(f"Error during org setup: {e}")
        return jsonify({"error": "Organization setup failed"}), 500


@auth_bp.route('/login', methods=['POST'])
def login():
    """Authenticate user with email and password."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Invalid request body"}), 400
        
        email = data.get('email')
        password = data.get('password')
        
        if not email or not password:
            return jsonify({"error": "Email and password are required"}), 400
        
        # Look up user in database
        conn = connect_to_db_as_user()
        try:
            with conn.cursor() as cursor:
                # No RLS needed — users not RLS-protected
                cursor.execute(
                    "SELECT u.id, u.email, u.name, u.password_hash, u.role, u.org_id, o.name, "
                    "COALESCE(u.must_change_password, FALSE) "
                    "FROM users u LEFT JOIN organizations o ON u.org_id = o.id "
                    "WHERE u.email = %s",
                    (email,)
                )
                user = cursor.fetchone()
                
                # Always perform password check to prevent timing attacks
                # Use dummy hash if user doesn't exist
                if user:
                    user_id, user_email, user_name, password_hash, user_role, user_org_id, user_org_name, must_change_pw = user
                else:
                    # Dummy hash to maintain consistent timing
                    password_hash = _DUMMY_BCRYPT_HASH
                
                # Verify password (runs regardless of whether user exists)
                password_valid = bcrypt.checkpw(password.encode('utf-8'), password_hash.encode('utf-8'))
                
                if not user or not password_valid:
                    return jsonify({"error": "Invalid credentials"}), 401
                
                logging.info(f"User logged in: {email}")

                record_audit_event(user_org_id or "", user_id, "login", "session", user_id, {"email": email}, request)

                return jsonify({
                    "id": user_id,
                    "email": user_email,
                    "name": user_name,
                    "role": user_role or "viewer",
                    "orgId": user_org_id,
                    "orgName": user_org_name,
                    "mustChangePassword": bool(must_change_pw),
                }), 200
        finally:
            conn.close()
            
    except Exception as e:
        logging.error(f"Error during login: {e}")
        return jsonify({"error": "Login failed"}), 500


@auth_bp.route('/change-password', methods=['POST'])
@require_auth_only
def change_password(user_id):
    """Change user password (requires authentication)."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Invalid request body"}), 400
        
        current_password = data.get('currentPassword')
        new_password = data.get('newPassword')
        
        if not current_password or not new_password:
            return jsonify({"error": "Current and new password are required"}), 400
            
        if len(new_password) < 8:
            return jsonify({"error": "New password must be at least 8 characters"}), 400
        
        # Verify current password and update
        conn = connect_to_db_as_user()
        try:
            with conn.cursor() as cursor:
                # No RLS needed — users not RLS-protected
                cursor.execute(
                    "SELECT password_hash FROM users WHERE id = %s",
                    (user_id,)
                )
                result = cursor.fetchone()
                
                if not result:
                    return jsonify({"error": "User not found"}), 404
                
                password_hash = result[0]
                
                # Verify current password
                if not bcrypt.checkpw(current_password.encode('utf-8'), password_hash.encode('utf-8')):
                    return jsonify({"error": "Current password is incorrect"}), 401
                
                # Hash and update new password
                new_password_hash = bcrypt.hashpw(new_password.encode('utf-8'), bcrypt.gensalt())
                cursor.execute(
                    "UPDATE users SET password_hash = %s, must_change_password = FALSE WHERE id = %s",
                    (new_password_hash.decode('utf-8'), user_id)
                )
                conn.commit()
                
                logging.info(f"Password changed for user: {user_id}")

                from utils.auth.stateless_auth import get_org_id_from_request as _get_org
                _org = _get_org() or ""
                record_audit_event(_org, user_id, "change_password", "user", user_id, {}, request)

                return jsonify({"message": "Password changed successfully"}), 200
        finally:
            conn.close()
            
    except Exception as e:
        logging.error(f"Error changing password: {e}")
        return jsonify({"error": "Password change failed"}), 500


@auth_bp.route('/me', methods=['GET'])
@require_auth_only
def get_current_user(user_id):
    """Return the current user's role and org from the database.

    Called periodically by the frontend JWT callback to keep the
    session in sync after admin role changes.
    """
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                # No RLS needed — users, organizations not RLS-protected
                cursor.execute(
                    "SELECT u.role, u.org_id, o.name, COALESCE(u.must_change_password, FALSE) "
                    "FROM users u LEFT JOIN organizations o ON u.org_id = o.id "
                    "WHERE u.id = %s",
                    (user_id,),
                )
                row = cursor.fetchone()
                if not row:
                    return jsonify({"error": "User not found"}), 404

                return jsonify({
                    "role": row[0] or "viewer",
                    "orgId": row[1],
                    "orgName": row[2],
                    "mustChangePassword": bool(row[3]),
                }), 200
    except Exception:
        logging.exception("Error in /me")
        return jsonify({"error": "Server error"}), 500


@auth_bp.route('/admins', methods=['GET'])
@require_auth_only
def get_admins(user_id):
    """Return the list of admin users (name + email only). Any authenticated user may call this."""
    from utils.auth.stateless_auth import get_org_id_from_request

    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "Organization context required"}), 403

    conn = connect_to_db_as_user()
    try:
        with conn.cursor() as cursor:
            # No RLS needed — users not RLS-protected
            cursor.execute(
                "SELECT name, email FROM users WHERE role = 'admin' AND org_id = %s ORDER BY created_at",
                (org_id,),
            )
            rows = cursor.fetchall()
        return jsonify([{"name": r[0], "email": r[1]} for r in rows]), 200
    except Exception as e:
        logging.exception("Error fetching admins for org %s: %s", org_id, e)
        return jsonify({"error": "Failed to fetch admins"}), 500
    finally:
        conn.close()
