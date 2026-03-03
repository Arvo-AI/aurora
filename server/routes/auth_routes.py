"""
Auth routes for user registration, login, and password management.
Replaces the previous authentication system.
"""
import logging
import bcrypt
from flask import Blueprint, request, jsonify
from utils.db.db_utils import connect_to_db_as_user
from utils.web.cors_utils import create_cors_response
import os

auth_bp = Blueprint('auth', __name__, url_prefix='/api/auth')

FRONTEND_URL = os.getenv("FRONTEND_URL")

@auth_bp.after_request
def add_cors_headers(response):
    """Add CORS headers to all responses from auth routes."""
    origin = request.headers.get('Origin', FRONTEND_URL)
    response.headers['Access-Control-Allow-Origin'] = origin
    response.headers['Access-Control-Allow-Credentials'] = 'true'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-Provider, X-Requested-With, X-User-ID, Authorization'
    return response

@auth_bp.route('/register', methods=['POST', 'OPTIONS'])
def register():
    """Register a new user with email and password."""
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Invalid request body"}), 400
        
        email = data.get('email')
        password = data.get('password')
        name = data.get('name')
        
        if not email or not password:
            return jsonify({"error": "Email and password are required"}), 400
            
        if len(password) < 8:
            return jsonify({"error": "Password must be at least 8 characters"}), 400
        
        # Hash password with bcrypt
        password_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
        
        # Store user in database
        conn = connect_to_db_as_user()
        try:
            with conn.cursor() as cursor:
                # Check if user already exists
                cursor.execute(
                    "SELECT id FROM users WHERE email = %s",
                    (email,)
                )
                if cursor.fetchone():
                    return jsonify({"error": "User with this email already exists"}), 409
                
                # Insert new user
                cursor.execute(
                    """
                    INSERT INTO users (email, password_hash, name, created_at)
                    VALUES (%s, %s, %s, NOW())
                    RETURNING id, email, name
                    """,
                    (email, password_hash.decode('utf-8'), name)
                )
                user = cursor.fetchone()
                user_id, user_email, user_name = user[0], user[1], user[2]

                # Auto-promote the very first user to admin
                cursor.execute("SELECT COUNT(*) FROM users")
                user_count = cursor.fetchone()[0]
                role = "admin" if user_count == 1 else "viewer"

                cursor.execute(
                    "UPDATE users SET role = %s WHERE id = %s",
                    (role, user_id)
                )

                # Auto-create or assign org
                org_id = None
                org_name = None
                if user_count == 1:
                    # First user: create default organization
                    cursor.execute(
                        """
                        INSERT INTO organizations (id, name, slug, created_by)
                        VALUES (gen_random_uuid()::TEXT, 'Default Organization', 'default', %s)
                        ON CONFLICT (slug) DO UPDATE SET slug = organizations.slug
                        RETURNING id, name
                        """,
                        (user_id,)
                    )
                    org_row = cursor.fetchone()
                    org_id, org_name = org_row[0], org_row[1]
                    cursor.execute(
                        "UPDATE users SET org_id = %s WHERE id = %s",
                        (org_id, user_id)
                    )
                else:
                    # Subsequent users: assign to the first (default) org
                    # NOTE: In a multi-tenant production environment, replace this
                    # with an invitation flow so strangers cannot self-register
                    # into an existing organization.
                    cursor.execute(
                        "SELECT id, name FROM organizations ORDER BY created_at ASC LIMIT 1"
                    )
                    org_row = cursor.fetchone()
                    if org_row:
                        org_id, org_name = org_row[0], org_row[1]
                        cursor.execute(
                            "UPDATE users SET org_id = %s WHERE id = %s",
                            (org_id, user_id)
                        )

                conn.commit()

                # Register the user-role mapping in Casbin (domain-aware)
                try:
                    from utils.auth.enforcer import assign_role_to_user
                    if org_id:
                        assign_role_to_user(user_id, role, org_id)
                    else:
                        from utils.auth.enforcer import get_enforcer
                        enforcer = get_enforcer()
                        enforcer.add_grouping_policy(user_id, role, "*")
                        enforcer.save_policy()
                except Exception as casbin_err:
                    logging.warning(f"Failed to assign Casbin role for {user_id}: {casbin_err}")
                
                logging.info(f"New user registered: {email} (role={role}, org={org_id})")
                
                return jsonify({
                    "id": user_id,
                    "email": user_email,
                    "name": user_name,
                    "role": role,
                    "orgId": org_id,
                    "orgName": org_name,
                }), 201
        finally:
            conn.close()
            
    except Exception as e:
        logging.error(f"Error during registration: {e}")
        return jsonify({"error": "Registration failed"}), 500


@auth_bp.route('/login', methods=['POST', 'OPTIONS'])
def login():
    """Authenticate user with email and password."""
    if request.method == 'OPTIONS':
        return create_cors_response()
    
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
                cursor.execute(
                    "SELECT u.id, u.email, u.name, u.password_hash, u.role, u.org_id, o.name "
                    "FROM users u LEFT JOIN organizations o ON u.org_id = o.id "
                    "WHERE u.email = %s",
                    (email,)
                )
                user = cursor.fetchone()
                
                # Always perform password check to prevent timing attacks
                # Use dummy hash if user doesn't exist
                if user:
                    user_id, user_email, user_name, password_hash, user_role, user_org_id, user_org_name = user
                else:
                    # Dummy hash to maintain consistent timing
                    password_hash = bcrypt.hashpw(b'dummy', bcrypt.gensalt()).decode('utf-8')
                
                # Verify password (runs regardless of whether user exists)
                password_valid = bcrypt.checkpw(password.encode('utf-8'), password_hash.encode('utf-8'))
                
                if not user or not password_valid:
                    return jsonify({"error": "Invalid credentials"}), 401
                
                logging.info(f"User logged in: {email}")
                
                return jsonify({
                    "id": user_id,
                    "email": user_email,
                    "name": user_name,
                    "role": user_role or "viewer",
                    "orgId": user_org_id,
                    "orgName": user_org_name,
                }), 200
        finally:
            conn.close()
            
    except Exception as e:
        logging.error(f"Error during login: {e}")
        return jsonify({"error": "Login failed"}), 500


@auth_bp.route('/change-password', methods=['POST', 'OPTIONS'])
def change_password():
    """Change user password (requires authentication)."""
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    try:
        user_id = request.headers.get('X-User-ID')
        if not user_id:
            return jsonify({"error": "Authentication required"}), 401
        
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
                    "UPDATE users SET password_hash = %s WHERE id = %s",
                    (new_password_hash.decode('utf-8'), user_id)
                )
                conn.commit()
                
                logging.info(f"Password changed for user: {user_id}")
                
                return jsonify({"message": "Password changed successfully"}), 200
        finally:
            conn.close()
            
    except Exception as e:
        logging.error(f"Error changing password: {e}")
        return jsonify({"error": "Password change failed"}), 500


@auth_bp.route('/admins', methods=['GET'])
def get_admins():
    """Return the list of admin users (name + email only). Any authenticated user may call this."""
    from utils.auth.stateless_auth import get_user_id_from_request, get_org_id_from_request
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401

    org_id = get_org_id_from_request()

    conn = connect_to_db_as_user()
    try:
        with conn.cursor() as cursor:
            if org_id:
                cursor.execute(
                    "SELECT name, email FROM users WHERE role = 'admin' AND org_id = %s ORDER BY created_at",
                    (org_id,),
                )
            else:
                cursor.execute(
                    "SELECT name, email FROM users WHERE role = 'admin' ORDER BY created_at"
                )
            rows = cursor.fetchall()
        return jsonify([{"name": r[0], "email": r[1]} for r in rows]), 200
    finally:
        conn.close()
