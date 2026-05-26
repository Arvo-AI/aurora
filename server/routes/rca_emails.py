"""RCA notification email management API routes (org-scoped)."""
import logging
import secrets
import re
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify
from utils.auth.stateless_auth import set_rls_context, get_org_id_from_request
from utils.auth.rbac_decorators import require_permission
from utils.db.connection_pool import db_pool
from utils.notifications.email_service import get_email_service
from routes.audit_routes import record_audit_event

logger = logging.getLogger(__name__)

rca_emails_bp = Blueprint('rca_emails', __name__)
_LOG_PREFIX = "[RCAEmails]"

EMAIL_REGEX = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')

MAX_RESEND_PER_HOUR = 5
CODE_EXPIRATION_MINUTES = 15


def _validate_email(email: str) -> bool:
    return bool(EMAIL_REGEX.match(email))


def _generate_verification_code() -> str:
    return f"{secrets.randbelow(1000000):06d}"


def _get_org_id(user_id: str) -> str | None:
    """Resolve org_id from request header or DB lookup."""
    org_id = get_org_id_from_request()
    if org_id:
        return org_id
    from utils.auth.stateless_auth import get_org_id_for_user
    return get_org_id_for_user(user_id)


@rca_emails_bp.route('/api/rca-emails', methods=['GET'])
@require_permission("rca_emails", "read")
def list_rca_emails(user_id):
    """List all notification recipient emails for the org."""
    try:
        org_id = _get_org_id(user_id)
        if not org_id:
            return jsonify({"error": "Could not resolve organization"}), 400

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix=_LOG_PREFIX)
                cursor.execute(
                    """
                    SELECT id, email, is_verified, created_at, verified_at, is_enabled
                    FROM rca_notification_emails
                    WHERE org_id = %s
                    ORDER BY created_at DESC
                    """,
                    (org_id,)
                )
                rows = cursor.fetchall()

                emails = [
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

        return jsonify({"emails": emails})

    except Exception as e:
        logger.error(f"{_LOG_PREFIX} Error listing emails for org: {e}")
        return jsonify({"error": "Failed to retrieve emails"}), 500


@rca_emails_bp.route('/api/rca-emails/add', methods=['POST'])
@require_permission("rca_emails", "write")
def add_rca_email(user_id):
    """Add a new notification recipient email for the org."""
    data = request.get_json()
    email = data.get('email', '').strip().lower()

    if not email:
        return jsonify({"error": "Email is required"}), 400

    if not _validate_email(email):
        return jsonify({"error": "Invalid email format"}), 400

    org_id = _get_org_id(user_id)
    if not org_id:
        return jsonify({"error": "Could not resolve organization"}), 400

    try:
        verification_code = _generate_verification_code()
        expires_at = datetime.now() + timedelta(minutes=CODE_EXPIRATION_MINUTES)

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix=_LOG_PREFIX)

                cursor.execute(
                    "SELECT id, is_verified FROM rca_notification_emails WHERE org_id = %s AND email = %s",
                    (org_id, email)
                )
                existing = cursor.fetchone()

                if existing:
                    if existing[1]:
                        return jsonify({"error": "This email is already configured and verified"}), 400

                    cursor.execute(
                        """
                        UPDATE rca_notification_emails
                        SET verification_code = %s, verification_code_expires_at = %s, created_at = %s
                        WHERE org_id = %s AND email = %s
                        """,
                        (verification_code, expires_at, datetime.now(), org_id, email)
                    )
                else:
                    cursor.execute(
                        """
                        INSERT INTO rca_notification_emails
                        (user_id, org_id, email, verification_code, verification_code_expires_at)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (user_id, org_id, email, verification_code, expires_at)
                    )

                conn.commit()

        try:
            email_service = get_email_service()
            success = email_service.send_verification_code_email(email, verification_code)
            if not success:
                return jsonify({"error": "Failed to send verification email"}), 500
        except ValueError as e:
            logger.error(f"{_LOG_PREFIX} Email service not configured: {e}")
            return jsonify({"error": "Email service not configured"}), 500

        logger.info("%s Verification code sent", _LOG_PREFIX)
        record_audit_event("", user_id, "add_notification_email", "rca_email", email, {"email": email}, request)
        return jsonify({"status": "success", "message": "Verification code sent to email"})

    except Exception as e:
        logger.error(f"{_LOG_PREFIX} Error adding email: {e}", exc_info=True)
        return jsonify({"error": "Failed to add email"}), 500


@rca_emails_bp.route('/api/rca-emails/verify', methods=['POST'])
@require_permission("rca_emails", "write")
def verify_rca_email(user_id):
    """Verify an email with the provided code."""
    data = request.get_json()
    email = data.get('email', '').strip().lower()
    code = data.get('code', '').strip()

    if not email or not code:
        return jsonify({"error": "Email and code are required"}), 400

    if len(code) != 6 or not code.isdigit():
        return jsonify({"error": "Invalid verification code format"}), 400

    org_id = _get_org_id(user_id)
    if not org_id:
        return jsonify({"error": "Could not resolve organization"}), 400

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix=_LOG_PREFIX)

                cursor.execute(
                    """
                    SELECT id, verification_code, verification_code_expires_at, is_verified
                    FROM rca_notification_emails
                    WHERE org_id = %s AND email = %s
                    """,
                    (org_id, email)
                )
                row = cursor.fetchone()

                if not row:
                    return jsonify({"error": "Email not found"}), 404

                email_id, stored_code, expires_at, is_verified = row

                if is_verified:
                    return jsonify({"error": "Email is already verified"}), 400

                if expires_at and datetime.now() > expires_at:
                    return jsonify({"error": "Verification code has expired"}), 400

                if stored_code != code:
                    return jsonify({"error": "Invalid verification code"}), 400

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

        logger.info("%s Email verified", _LOG_PREFIX)
        record_audit_event("", user_id, "verify_notification_email", "rca_email", email, {"email": email}, request)
        return jsonify({"status": "success", "message": "Email verified successfully"})

    except Exception as e:
        logger.error(f"{_LOG_PREFIX} Error verifying email: {e}")
        return jsonify({"error": "Failed to verify email"}), 500


@rca_emails_bp.route('/api/rca-emails/resend', methods=['POST'])
@require_permission("rca_emails", "write")
def resend_verification_code(user_id):
    """Resend verification code to an email."""
    data = request.get_json()
    email = data.get('email', '').strip().lower()

    if not email:
        return jsonify({"error": "Email is required"}), 400

    org_id = _get_org_id(user_id)
    if not org_id:
        return jsonify({"error": "Could not resolve organization"}), 400

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix=_LOG_PREFIX)

                cursor.execute(
                    """
                    SELECT id, is_verified FROM rca_notification_emails
                    WHERE org_id = %s AND email = %s
                    """,
                    (org_id, email)
                )
                row = cursor.fetchone()

                if not row:
                    return jsonify({"error": "Email not found"}), 404

                if row[1]:
                    return jsonify({"error": "Email is already verified"}), 400

                verification_code = _generate_verification_code()
                expires_at = datetime.now() + timedelta(minutes=CODE_EXPIRATION_MINUTES)

                cursor.execute(
                    """
                    UPDATE rca_notification_emails
                    SET verification_code = %s, verification_code_expires_at = %s
                    WHERE org_id = %s AND email = %s
                    """,
                    (verification_code, expires_at, org_id, email)
                )
                conn.commit()

        try:
            email_service = get_email_service()
            success = email_service.send_verification_code_email(email, verification_code)
            if not success:
                return jsonify({"error": "Failed to send verification email"}), 500
        except ValueError as e:
            logger.error(f"{_LOG_PREFIX} Email service not configured: {e}")
            return jsonify({"error": "Email service not configured"}), 500

        logger.info("%s Verification code resent", _LOG_PREFIX)
        return jsonify({"status": "success", "message": "Verification code resent"})

    except Exception as e:
        logger.error(f"{_LOG_PREFIX} Error resending code: {e}")
        return jsonify({"error": "Failed to resend verification code"}), 500


@rca_emails_bp.route('/api/rca-emails/<int:email_id>/toggle', methods=['POST'])
@require_permission("rca_emails", "write")
def toggle_rca_email(user_id, email_id: int):
    """Toggle an email address enabled/disabled status."""
    data = request.get_json()
    is_enabled = data.get('is_enabled')

    if is_enabled is None:
        return jsonify({"error": "is_enabled field is required"}), 400

    org_id = _get_org_id(user_id)
    if not org_id:
        return jsonify({"error": "Could not resolve organization"}), 400

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix=_LOG_PREFIX)

                cursor.execute(
                    """
                    UPDATE rca_notification_emails
                    SET is_enabled = %s
                    WHERE id = %s AND org_id = %s AND is_verified = TRUE
                    """,
                    (is_enabled, email_id, org_id)
                )
                if cursor.rowcount == 0:
                    return jsonify({"error": "Email not found or not verified"}), 404
                conn.commit()

        logger.info("%s Email toggled", _LOG_PREFIX)
        record_audit_event("", user_id, "toggle_notification_email", "rca_email", str(email_id),
                           {"is_enabled": is_enabled}, request)
        return jsonify({"status": "success"})

    except Exception as e:
        logger.error(f"{_LOG_PREFIX} Error toggling email: {e}")
        return jsonify({"error": "Failed to toggle email"}), 500


@rca_emails_bp.route('/api/rca-emails/<int:email_id>', methods=['DELETE'])
@require_permission("rca_emails", "write")
def remove_rca_email(user_id, email_id: int):
    """Remove a notification recipient email."""
    org_id = _get_org_id(user_id)
    if not org_id:
        return jsonify({"error": "Could not resolve organization"}), 400

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix=_LOG_PREFIX)

                cursor.execute(
                    "DELETE FROM rca_notification_emails WHERE id = %s AND org_id = %s",
                    (email_id, org_id)
                )
                if cursor.rowcount == 0:
                    return jsonify({"error": "Email not found"}), 404
                conn.commit()

        logger.info("%s Email removed", _LOG_PREFIX)
        record_audit_event("", user_id, "remove_notification_email", "rca_email", str(email_id), {}, request)
        return jsonify({"status": "success"})

    except Exception as e:
        logger.error(f"{_LOG_PREFIX} Error removing email: {e}")
        return jsonify({"error": "Failed to remove email"}), 500
