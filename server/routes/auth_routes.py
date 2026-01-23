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
                conn.commit()
                
                logging.info(f"New user registered: {email}")
                
                return jsonify({
                    "id": user[0],
                    "email": user[1],
                    "name": user[2]
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
                    "SELECT id, email, name, password_hash FROM users WHERE email = %s",
                    (email,)
                )
                user = cursor.fetchone()
                
                # Always perform password check to prevent timing attacks
                # Use dummy hash if user doesn't exist
                if user:
                    user_id, user_email, user_name, password_hash = user
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
                    "name": user_name
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
