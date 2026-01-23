"""RCA notification email management API routes."""
import logging
import secrets
import re
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify
from utils.auth.stateless_auth import (
    get_user_id_from_request,
    get_user_email,
    create_cors_response
)
from utils.db.connection_pool import db_pool
from utils.notifications.email_service import get_email_service

logger = logging.getLogger(__name__)

rca_emails_bp = Blueprint('rca_emails', __name__)

# Email validation regex
EMAIL_REGEX = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')

# Rate limiting constants
MAX_VERIFICATION_ATTEMPTS = 3
MAX_RESEND_PER_HOUR = 5
CODE_EXPIRATION_MINUTES = 15


def _validate_email(email: str) -> bool:
    """Validate email format."""
    return bool(EMAIL_REGEX.match(email))


def _generate_verification_code() -> str:
    """Generate a random 6-digit verification code."""
    return f"{secrets.randbelow(1000000):06d}"


def _check_rate_limit(user_id: str, email: str, action: str) -> tuple[bool, str]:
    """
    Check rate limits for verification actions.
    
    Args:
        user_id: User ID
        email: Email address
        action: 'add' or 'resend'
    
    Returns:
        Tuple of (is_allowed, error_message)
    """
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                if action == 'resend':
                    # Check resend rate limit (5 per hour)
                    one_hour_ago = datetime.now() - timedelta(hours=1)
                    cursor.execute(
                        """
                        SELECT COUNT(*) FROM rca_notification_emails
                        WHERE user_id = %s AND email = %s AND created_at > %s
                        """,
                        (user_id, email, one_hour_ago)
                    )
                    count = cursor.fetchone()[0]
                    if count >= MAX_RESEND_PER_HOUR:
                        return False, "Too many verification attempts. Please wait an hour."
                
                return True, ""
    except Exception as e:
        logger.error(f"[RCAEmails] Error checking rate limit: {e}")
        return True, ""  # Allow on error to avoid blocking legitimate users


@rca_emails_bp.route('/api/rca-emails', methods=['GET', 'OPTIONS'])
def list_rca_emails():
    """List all emails for user (primary + additional)."""
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400
    
    try:
        # Get primary email from Auth.js or database
        primary_email = get_user_email(user_id)
        
        # Get additional emails from database
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
                cursor.execute(
                    """
                    SELECT id, email, is_verified, created_at, verified_at, is_enabled
                    FROM rca_notification_emails
                    WHERE user_id = %s
                    ORDER BY created_at DESC
                    """,
                    (user_id,)
                )
                rows = cursor.fetchall()
                
                additional_emails = [
                    {
                        "id": row[0],
                        "email": row[1],
                        "is_verified": row[2],
                        "created_at": row[3].isoformat() if row[3] else None,
                        "verified_at": row[4].isoformat() if row[4] else None,
                        "is_enabled": row[5] if row[5] is not None else True,
                    }
                    for row in rows
                ]
        
        return jsonify({
            "primary_email": primary_email,
            "additional_emails": additional_emails
        })
    
    except Exception as e:
        logger.error(f"[RCAEmails] Error listing emails for user {user_id}: {e}")
        return jsonify({"error": "Failed to retrieve emails"}), 500


@rca_emails_bp.route('/api/rca-emails/add', methods=['POST', 'OPTIONS'])
def add_rca_email():
    """Add a new email and send verification code."""
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400
    
    data = request.get_json()
    email = data.get('email', '').strip().lower()
    
    if not email:
        return jsonify({"error": "Email is required"}), 400
    
    if not _validate_email(email):
        return jsonify({"error": "Invalid email format"}), 400
    
    # Check rate limit
    is_allowed, error_msg = _check_rate_limit(user_id, email, 'add')
    if not is_allowed:
        return jsonify({"error": error_msg}), 429
    
    try:
        # Check if user already has this email
        primary_email = get_user_email(user_id)
        if email == primary_email:
            return jsonify({"error": "This is your primary email. It already receives notifications."}), 400
        
        # Generate verification code
        verification_code = _generate_verification_code()
        expires_at = datetime.now() + timedelta(minutes=CODE_EXPIRATION_MINUTES)
        
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
                
                # Check if email already exists for this user
                cursor.execute(
                    "SELECT id, is_verified FROM rca_notification_emails WHERE user_id = %s AND email = %s",
                    (user_id, email)
                )
                existing = cursor.fetchone()
                
                if existing:
                    if existing[1]:  # is_verified
                        return jsonify({"error": "This email is already verified"}), 400
                    
                    # Update existing unverified email with new code
                    cursor.execute(
                        """
                        UPDATE rca_notification_emails
                        SET verification_code = %s, verification_code_expires_at = %s, created_at = %s
                        WHERE user_id = %s AND email = %s
                        """,
                        (verification_code, expires_at, datetime.now(), user_id, email)
                    )
                else:
                    # Insert new email
                    cursor.execute(
                        """
                        INSERT INTO rca_notification_emails 
                        (user_id, email, verification_code, verification_code_expires_at)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (user_id, email, verification_code, expires_at)
                    )
                
                conn.commit()
        
        # Send verification email
        try:
            email_service = get_email_service()
            success = email_service.send_verification_code_email(email, verification_code)
            if not success:
                logger.warning(f"[RCAEmails] Failed to send verification email to {email}")
                return jsonify({"error": "Failed to send verification email"}), 500
        except ValueError as e:
            logger.error(f"[RCAEmails] Email service not configured: {e}")
            return jsonify({"error": "Email service not configured"}), 500
        
        logger.info(f"[RCAEmails] Verification code sent to {email} for user {user_id}")
        return jsonify({
            "status": "success",
            "message": "Verification code sent to email"
        })
    
    except Exception as e:
        logger.error(f"[RCAEmails] Error adding email for user {user_id}: {e}", exc_info=True)
        return jsonify({"error": "Failed to add email"}), 500


@rca_emails_bp.route('/api/rca-emails/verify', methods=['POST', 'OPTIONS'])
def verify_rca_email():
    """Verify an email with the provided code."""
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400
    
    data = request.get_json()
    email = data.get('email', '').strip().lower()
    code = data.get('code', '').strip()
    
    if not email or not code:
        return jsonify({"error": "Email and code are required"}), 400
    
    if len(code) != 6 or not code.isdigit():
        return jsonify({"error": "Invalid verification code format"}), 400
    
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
                
                # Get email record
                cursor.execute(
                    """
                    SELECT id, verification_code, verification_code_expires_at, is_verified
                    FROM rca_notification_emails
                    WHERE user_id = %s AND email = %s
                    """,
                    (user_id, email)
                )
                row = cursor.fetchone()
                
                if not row:
                    return jsonify({"error": "Email not found"}), 404
                
                email_id, stored_code, expires_at, is_verified = row
                
                if is_verified:
                    return jsonify({"error": "Email is already verified"}), 400
                
                # Check if code expired
                if expires_at and datetime.now() > expires_at:
                    return jsonify({"error": "Verification code has expired"}), 400
                
                # Verify code
                if stored_code != code:
                    return jsonify({"error": "Invalid verification code"}), 400
                
                # Mark as verified
                cursor.execute(
                    """
                    UPDATE rca_notification_emails
                    SET is_verified = TRUE, verified_at = %s, 
                        verification_code = NULL, verification_code_expires_at = NULL
                    WHERE id = %s
                    """,
                    (datetime.now(), email_id)
                )
                conn.commit()
        
        logger.info(f"[RCAEmails] Email {email} verified for user {user_id}")
        return jsonify({
            "status": "success",
            "message": "Email verified successfully"
        })
    
    except Exception as e:
        logger.error(f"[RCAEmails] Error verifying email for user {user_id}: {e}")
        return jsonify({"error": "Failed to verify email"}), 500


@rca_emails_bp.route('/api/rca-emails/resend', methods=['POST', 'OPTIONS'])
def resend_verification_code():
    """Resend verification code to an email."""
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400
    
    data = request.get_json()
    email = data.get('email', '').strip().lower()
    
    if not email:
        return jsonify({"error": "Email is required"}), 400
    
    # Check rate limit
    is_allowed, error_msg = _check_rate_limit(user_id, email, 'resend')
    if not is_allowed:
        return jsonify({"error": error_msg}), 429
    
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
                
                # Check if email exists and is not verified
                cursor.execute(
                    """
                    SELECT id, is_verified FROM rca_notification_emails
                    WHERE user_id = %s AND email = %s
                    """,
                    (user_id, email)
                )
                row = cursor.fetchone()
                
                if not row:
                    return jsonify({"error": "Email not found"}), 404
                
                if row[1]:  # is_verified
                    return jsonify({"error": "Email is already verified"}), 400
                
                # Generate new code
                verification_code = _generate_verification_code()
                expires_at = datetime.now() + timedelta(minutes=CODE_EXPIRATION_MINUTES)
                
                cursor.execute(
                    """
                    UPDATE rca_notification_emails
                    SET verification_code = %s, verification_code_expires_at = %s
                    WHERE user_id = %s AND email = %s
                    """,
                    (verification_code, expires_at, user_id, email)
                )
                conn.commit()
        
        # Send verification email
        try:
            email_service = get_email_service()
            success = email_service.send_verification_code_email(email, verification_code)
            if not success:
                logger.warning(f"[RCAEmails] Failed to resend verification email to {email}")
                return jsonify({"error": "Failed to send verification email"}), 500
        except ValueError as e:
            logger.error(f"[RCAEmails] Email service not configured: {e}")
            return jsonify({"error": "Email service not configured"}), 500
        
        logger.info(f"[RCAEmails] Verification code resent to {email} for user {user_id}")
        return jsonify({
            "status": "success",
            "message": "Verification code resent"
        })
    
    except Exception as e:
        logger.error(f"[RCAEmails] Error resending code for user {user_id}: {e}")
        return jsonify({"error": "Failed to resend verification code"}), 500


@rca_emails_bp.route('/api/rca-emails/<int:email_id>/toggle', methods=['POST', 'OPTIONS'])
def toggle_rca_email(email_id: int):
    """Toggle an email address enabled/disabled status."""
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400
    
    data = request.get_json()
    is_enabled = data.get('is_enabled')
    
    if is_enabled is None:
        return jsonify({"error": "is_enabled field is required"}), 400
    
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
                
                # Update enabled status
                cursor.execute(
                    """
                    UPDATE rca_notification_emails 
                    SET is_enabled = %s
                    WHERE id = %s AND user_id = %s AND is_verified = TRUE
                    """,
                    (is_enabled, email_id, user_id)
                )
                rows_updated = cursor.rowcount
                conn.commit()
                
                if rows_updated == 0:
                    return jsonify({"error": "Email not found or not verified"}), 404
        
        logger.info(f"[RCAEmails] Email ID {email_id} toggled to {is_enabled} for user {user_id}")
        return jsonify({
            "status": "success",
            "message": f"Email {'enabled' if is_enabled else 'disabled'} successfully"
        })
    
    except Exception as e:
        logger.error(f"[RCAEmails] Error toggling email for user {user_id}: {e}")
        return jsonify({"error": "Failed to toggle email"}), 500


@rca_emails_bp.route('/api/rca-emails/<int:email_id>', methods=['DELETE', 'OPTIONS'])
def remove_rca_email(email_id: int):
    """Remove an additional email."""
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400
    
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
                
                # Delete email
                cursor.execute(
                    "DELETE FROM rca_notification_emails WHERE id = %s AND user_id = %s",
                    (email_id, user_id)
                )
                rows_deleted = cursor.rowcount
                conn.commit()
                
                if rows_deleted == 0:
                    return jsonify({"error": "Email not found"}), 404
        
        logger.info(f"[RCAEmails] Email ID {email_id} removed for user {user_id}")
        return jsonify({
            "status": "success",
            "message": "Email removed successfully"
        })
    
    except Exception as e:
        logger.error(f"[RCAEmails] Error removing email for user {user_id}: {e}")
        return jsonify({"error": "Failed to remove email"}), 500

