"""
Auth routes for user registration, login, and password management.
Replaces the previous authentication system.
"""
import logging
from routes.audit_routes import record_audit_event
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta
import bcrypt
from flask import Blueprint, request, jsonify
from utils.db.db_utils import connect_to_db_as_user
from utils.db.connection_pool import db_pool
from utils.auth.rbac_decorators import require_auth_only
from utils.web.limiter_ext import get_public_auth_rate_limit_key, limiter
import os

auth_bp = Blueprint('auth', __name__, url_prefix='/api/auth')

_DUMMY_BCRYPT_HASH = bcrypt.hashpw(os.urandom(16), bcrypt.gensalt()).decode('utf-8')

FRONTEND_URL = os.getenv("FRONTEND_URL")

VERIFICATION_CODE_EXPIRY_MINUTES = 15
RESEND_COOLDOWN_MINUTES = 1
PASSWORD_RESET_CODE_EXPIRY_MINUTES = 15
PASSWORD_RESET_MAX_ATTEMPTS = 5
PASSWORD_RESET_RESEND_COOLDOWN_MINUTES = 1
_SERVER_ERROR = "Server error"

# Returned for every /forgot-password outcome — found, not found, or send
# failure — so the endpoint can't be used to enumerate which emails have
# accounts.
_RESET_REQUESTED_MESSAGE = (
    "If an account exists for that email, we've sent a reset code. "
    "Check your spam folder if you don't see it."
)

# One message for wrong, expired, never-issued, and unknown-account codes, so a
# guess can't be narrowed down by which rejection came back.
_RESET_CODE_INVALID = "Invalid or expired reset code"

SLUG_REGEX = re.compile(r'^[a-z0-9][a-z0-9-]{0,48}[a-z0-9]$')
ORG_NAME_REGEX = re.compile(r"^[\w\s\-\.,'&()]+$", re.UNICODE)
ORG_NAME_ERROR = "Organization name can only contain letters, numbers, spaces, hyphens, periods, commas, apostrophes, ampersands, and parentheses"

def _name_to_slug(name: str) -> str:
    slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:50]
    if len(slug) < 2:
        slug = slug + '-org'
    return slug


def _generate_code() -> tuple[str, str]:
    """Return a fresh 6-digit code and its SHA-256 hex digest.

    Only the digest is persisted, so a database read can't be replayed as a
    valid code.
    """
    code = f"{secrets.randbelow(1000000):06d}"
    return code, hashlib.sha256(code.encode()).hexdigest()


def _find_user_for_email(cursor, email: str):
    """Resolve an email to ``(user_id, email)``, or None when ambiguous/absent.

    Exact match wins. Only if nothing matches exactly do we retry
    case-insensitively, and then only when exactly one row matches: legacy
    mixed-case duplicates exist, and mailing a reset code for a row the sender
    didn't mean is worse than asking them to use the exact address they signed
    up with.
    """
    cursor.execute("SELECT id, email FROM users WHERE email = %s", (email,))
    row = cursor.fetchone()
    if row:
        return row

    # No exact row — fall back to a case-insensitive match.
    cursor.execute(
        "SELECT id, email FROM users WHERE LOWER(email) = LOWER(%s) LIMIT 2",
        (email,),
    )
    rows = cursor.fetchall()
    return rows[0] if len(rows) == 1 else None


def send_verification_email(user_id: str, email: str) -> bool:
    """Generate a verification code, store it, and email it to the user.

    If SMTP is not configured (ValueError from email service), auto-verifies
    the user so they aren't locked out.

    Returns True if the email was sent (or user was auto-verified), False on failure.
    """
    from utils.notifications.email_service import get_email_service

    try:
        email_svc = get_email_service()
    except ValueError:
        with db_pool.get_admin_connection() as c:
            with c.cursor() as cur:
                cur.execute("UPDATE users SET email_verified = TRUE WHERE id = %s", (user_id,))
                c.commit()
        return True

    code, code_hash = _generate_code()
    expires = datetime.now() + timedelta(minutes=VERIFICATION_CODE_EXPIRY_MINUTES)

    with db_pool.get_admin_connection() as c:
        with c.cursor() as cur:
            cur.execute(
                "UPDATE users SET email_verification_code = %s, "
                "email_verification_code_expires_at = %s, email_verification_attempts = 0 "
                "WHERE id = %s",
                (code_hash, expires, user_id),
            )
            c.commit()

    return email_svc.send_account_verification_email(email, code)


def _reset_code_is_fresh(user_id: str) -> bool:
    """True when this account was mailed a reset code within the cooldown.

    Rate limiting alone can't protect an individual mailbox: every request
    arrives from the frontend proxy's IP, so all users share one bucket.
    Fails open — a lookup error should not block a legitimate reset.
    """
    try:
        with db_pool.get_admin_connection() as c, c.cursor() as cur:
            cur.execute(
                "SELECT password_reset_code_expires_at FROM users WHERE id = %s",
                (user_id,),
            )
            row = cur.fetchone()
    except Exception:
        logging.exception("Could not read reset-code freshness for %s", user_id)
        return False

    if not row or not row[0]:
        return False

    # Derive when the code was issued from its expiry; a code younger than the
    # cooldown means we already mailed one moments ago.
    issued_at = row[0] - timedelta(minutes=PASSWORD_RESET_CODE_EXPIRY_MINUTES)
    return datetime.now() < issued_at + timedelta(
        minutes=PASSWORD_RESET_RESEND_COOLDOWN_MINUTES
    )


def send_password_reset_email(user_id: str, email: str) -> bool:
    """Generate a password reset code, store its hash, and email it.

    Unlike verification there is no auto-approve fallback when SMTP is
    unconfigured: a reset with no email to prove inbox ownership would let
    anyone take over any account. Callers get False and must surface an error.

    Returns True if the email was sent, False otherwise.
    """
    from utils.notifications.email_service import get_email_service

    try:
        email_svc = get_email_service()
    except ValueError:
        logging.warning("Password reset requested but SMTP is not configured")
        return False

    code, code_hash = _generate_code()
    expires = datetime.now() + timedelta(minutes=PASSWORD_RESET_CODE_EXPIRY_MINUTES)

    with db_pool.get_admin_connection() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE users SET password_reset_code = %s, "
            "password_reset_code_expires_at = %s, password_reset_attempts = 0 "
            "WHERE id = %s",
            (code_hash, expires, user_id),
        )
        c.commit()

    return email_svc.send_password_reset_email(email, code)


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

                try:
                    from utils.auth.command_policy import seed_default_command_policy
                    seed_default_command_policy(org_id, user_id)
                except Exception as policy_err:
                    logging.warning("Failed to seed command policy for org %s", org_id, exc_info=policy_err)

                try:
                    from utils.auth.tool_registry import seed_org_tool_permissions
                    seed_org_tool_permissions(org_id, user_id)
                except Exception as tool_perm_err:
                    logging.warning("Failed to seed tool permissions for org %s", org_id, exc_info=tool_perm_err)

                record_audit_event(org_id, user_id, "register", "organization", org_id,
                                   {"email": email}, request)

                try:
                    send_verification_email(user_id, email)
                except Exception:  # noqa: BLE001 — don't block registration if verification email fails
                    logging.warning("Failed to send verification email for %s", user_id)

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

                try:
                    from utils.auth.command_policy import seed_default_command_policy
                    seed_default_command_policy(org_id, user_id)
                except Exception as policy_err:
                    logging.warning("Failed to seed command policy for org %s", org_id, exc_info=policy_err)

                try:
                    from utils.auth.tool_registry import seed_org_tool_permissions
                    seed_org_tool_permissions(org_id, user_id)
                except Exception as tool_perm_err:
                    logging.warning("Failed to seed tool permissions for org %s", org_id, exc_info=tool_perm_err)

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


@auth_bp.route('/handoff', methods=['POST'])
@limiter.limit("10 per minute;30 per hour")
def exchange_handoff():
    """Exchange a one-time signup handoff token for a session payload.

    Body: { token }. Minted by the GitHub one-click signup callback
    (routes/github/github_signup.py) and redeemed exactly once by the
    frontend's NextAuth handler — the row is burned before the payload is
    returned, so a replayed token gets 401. Constant generic error for
    every failure mode (unknown/expired/used) to avoid oracle behavior.
    """
    from routes.github.github_signup import is_hosted_signup_enabled

    if not is_hosted_signup_enabled():
        return jsonify({"error": "Not available"}), 404

    try:
        data = request.get_json(silent=True) or {}
        token = data.get('token') or ''
        if not isinstance(token, str) or not (20 <= len(token) <= 128):
            return jsonify({"error": "Invalid token"}), 401

        token_hash = hashlib.sha256(token.encode()).hexdigest()

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                # Burn-then-read in one statement: the UPDATE only matches an
                # unexpired, unused hash, so concurrent redeems race on the
                # row lock and exactly one wins.
                cursor.execute(
                    """UPDATE users
                          SET signup_handoff_hash = NULL,
                              signup_handoff_expires_at = NULL
                        WHERE signup_handoff_hash = %s
                          AND signup_handoff_expires_at > NOW()
                       RETURNING id, email, name, role, org_id,
                                 COALESCE(must_change_password, FALSE),
                                 COALESCE(email_verified, FALSE),
                                 (github_user_id IS NOT NULL)""",
                    (token_hash,),
                )
                row = cursor.fetchone()
                if not row:
                    conn.commit()
                    return jsonify({"error": "Invalid token"}), 401
                (user_id, user_email, user_name, user_role, user_org_id,
                 must_change_pw, email_verified, is_github) = row
                cursor.execute(
                    "SELECT name FROM organizations WHERE id = %s", (user_org_id,)
                )
                org_row = cursor.fetchone()
                conn.commit()

        record_audit_event(
            user_org_id or "", user_id, "login", "session", user_id,
            {"via": "github_one_click_handoff"}, request,
        )
        return jsonify({
            "id": user_id,
            "email": user_email,
            "name": user_name,
            # Signup always writes role='admin'; if it is ever missing, fail
            # toward the least privilege the frontend middleware understands.
            "role": user_role or "viewer",
            "orgId": user_org_id,
            "orgName": org_row[0] if org_row else None,
            "mustChangePassword": bool(must_change_pw),
            "emailVerified": bool(email_verified),
            "isGithubProvisioned": bool(is_github),
        }), 200
    except Exception:
        logging.exception("Error during handoff exchange")
        return jsonify({"error": "Login failed"}), 500


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
                    "COALESCE(u.must_change_password, FALSE), COALESCE(u.email_verified, FALSE), "
                    "(u.github_user_id IS NOT NULL) "
                    "FROM users u LEFT JOIN organizations o ON u.org_id = o.id "
                    "WHERE u.email = %s",
                    (email,)
                )
                user = cursor.fetchone()

                # Always perform password check to prevent timing attacks
                # Use dummy hash if user doesn't exist
                if user:
                    user_id, user_email, user_name, password_hash, user_role, user_org_id, user_org_name, must_change_pw, email_verified, is_github = user
                else:
                    # Dummy hash to maintain consistent timing
                    password_hash = _DUMMY_BCRYPT_HASH
                
                # Verify password (runs regardless of whether user exists)
                password_valid = bcrypt.checkpw(password.encode('utf-8'), password_hash.encode('utf-8'))
                
                # Resolve audit identifiers before branching so the variable
                # lookups don't create a measurable timing difference.
                _audit_org = (user_org_id or "") if user else ""
                _audit_uid = user_id if user else ""
                _login_failed = not user or not password_valid

                # Always perform one DB round-trip (audit INSERT) regardless of
                # success/failure to preserve the timing-attack protection from
                # _DUMMY_BCRYPT_HASH.
                if _login_failed:
                    _detail = {"reason": "invalid_password", "email": email} if user else {
                        "reason": "unknown_email",
                        "email_sha256": hashlib.sha256(email[:254].lower().encode()).hexdigest(),
                    }
                    record_audit_event(
                        _audit_org, _audit_uid,
                        "login_failed", "session", None,
                        _detail,
                        request,
                    )
                    return jsonify({"error": "Invalid credentials"}), 401
                
                record_audit_event(_audit_org, _audit_uid, "login", "session", _audit_uid, {"email": email}, request)

                return jsonify({
                    "id": user_id,
                    "email": user_email,
                    "name": user_name,
                    "role": user_role or "viewer",
                    "orgId": user_org_id,
                    "orgName": user_org_name,
                    "mustChangePassword": bool(must_change_pw),
                    "emailVerified": bool(email_verified),
                    "isGithubProvisioned": bool(is_github),
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
        
        current_password = data.get('currentPassword') or ''
        new_password = data.get('newPassword')

        if not new_password:
            return jsonify({"error": "New password is required"}), 400

        if len(new_password) < 8:
            return jsonify({"error": "New password must be at least 8 characters"}), 400

        # Verify current password and update
        conn = connect_to_db_as_user()
        try:
            with conn.cursor() as cursor:
                # No RLS needed — users not RLS-protected
                cursor.execute(
                    "SELECT password_hash, email, COALESCE(must_change_password, FALSE), "
                    "COALESCE(email_verified, FALSE), github_user_id "
                    "FROM users WHERE id = %s",
                    (user_id,)
                )
                result = cursor.fetchone()

                if not result:
                    return jsonify({"error": "User not found"}), 404

                password_hash, user_email, was_must_change, was_verified, github_user_id = result

                from utils.auth.stateless_auth import resolve_org_id
                org_id = resolve_org_id(user_id) or ""

                # GitHub-provisioned users have an unusable password (hash of
                # random bytes). Allow them to set their first password without
                # providing the current one — but only while must_change_password
                # is still TRUE. Once they've set a real password the normal
                # current-password check applies.
                if current_password:
                    if not bcrypt.checkpw(current_password.encode('utf-8'), password_hash.encode('utf-8')):
                        record_audit_event(
                            org_id, user_id, "change_password_failed",
                            "user", user_id, {"reason": "wrong_current_password"}, request,
                        )
                        return jsonify({"error": "Current password is incorrect"}), 401
                elif not (github_user_id and was_must_change):
                    return jsonify({"error": "Current password is required"}), 400
                
                # Hash and update new password
                new_password_hash = bcrypt.hashpw(new_password.encode('utf-8'), bcrypt.gensalt())
                cursor.execute(
                    "UPDATE users SET password_hash = %s, must_change_password = FALSE WHERE id = %s",
                    (new_password_hash.decode('utf-8'), user_id)
                )
                conn.commit()
                
                logging.info(f"Password changed for user: {user_id}")

                record_audit_event(org_id, user_id, "change_password", "user", user_id, {}, request)

                if was_must_change and not was_verified:
                    send_verification_email(user_id, user_email)

                return jsonify({"message": "Password changed successfully"}), 200
        finally:
            conn.close()
            
    except Exception as e:
        logging.error(f"Error changing password: {e}")
        return jsonify({"error": "Password change failed"}), 500


def _dispatch_reset_code(email: str) -> None:
    """Best-effort: mail a reset code for ``email``. Never raises, returns nothing.

    Deliberately conveys no outcome to the caller. /forgot-password must answer
    identically whether or not an account exists, so there is nothing here worth
    returning — every branch is logged instead.
    """
    if not email or len(email) > 254:
        return

    try:
        with connect_to_db_as_user() as conn, conn.cursor() as cursor:
            # No RLS needed — users not RLS-protected
            row = _find_user_for_email(cursor, email)

        # No account, or an ambiguous case-variant match — say nothing.
        if not row:
            logging.info(
                "Password reset requested for an address with no unique account"
            )
            return

        user_id, user_email = row[0], row[1]

        # Per-account cooldown. The Flask-Limiter bucket on the route keys on the
        # caller IP, which is the frontend proxy for every user, so it can't stop
        # one address being mail-bombed.
        if _reset_code_is_fresh(user_id):
            logging.info("Suppressed duplicate password reset request for %s", user_id)
            return

        if not send_password_reset_email(user_id, user_email):
            logging.warning("Failed to send password reset email for user %s", user_id)
            return

        from utils.auth.stateless_auth import resolve_org_id
        record_audit_event(
            resolve_org_id(user_id) or "", user_id,
            "password_reset_requested", "user", user_id, {}, request,
        )
    except Exception:
        # Swallowed on purpose: a 500 here would distinguish existing accounts
        # from missing ones, which is exactly what the constant response prevents.
        logging.exception("Error dispatching password reset code")


@auth_bp.route('/password-reset-available', methods=['GET'])
@limiter.limit("30 per minute")
def password_reset_available():
    """Report whether this deployment can send password reset emails at all.

    Unauthenticated, and safe to be: the answer describes our SMTP config, not
    any account, so unlike a per-email answer it reveals nothing to enumerate.
    Without it the UI has no way to know a reset is impossible — /forgot-password
    deliberately answers 200 either way — so it would promise a code that never
    arrives.
    """
    from utils.notifications.email_service import is_email_configured

    return jsonify({"available": is_email_configured()}), 200


@auth_bp.route('/forgot-password', methods=['POST'])
# Keyed on the submitted email: these requests arrive via the frontend proxy, so
# the default IP key would be one shared bucket for every user.
@limiter.limit("5 per minute;20 per hour", key_func=get_public_auth_rate_limit_key)
def forgot_password():
    """Email a one-time reset code to the address in the body.

    Body: { email }. Unauthenticated by design — the code mailed to the address
    is the proof of ownership.

    Returns one constant 200 for every outcome — found, missing, ambiguous, send
    failure, internal error — so the endpoint can't be used to discover which
    emails have accounts. The real outcome is visible only in the audit log and
    server logs, which is why _dispatch_reset_code() reports nothing back.
    """
    data = request.get_json(silent=True) or {}
    raw_email = data.get('email')
    email = raw_email.strip() if isinstance(raw_email, str) else ''

    _dispatch_reset_code(email)

    return jsonify({"message": _RESET_REQUESTED_MESSAGE}), 200


def _parse_reset_request(data):
    """Validate a /reset-password body.

    Returns ``(fields, error)`` where exactly one is None — ``fields`` is
    ``(email, code, new_password)`` on success.
    """
    raw_email = data.get('email')
    email = raw_email.strip() if isinstance(raw_email, str) else ''
    raw_code = data.get('code')
    code = raw_code.strip() if isinstance(raw_code, str) else ''
    new_password = data.get('newPassword')

    if not email or not code:
        return None, (jsonify({"error": "Email and code are required"}), 400)

    if len(code) != 6 or not code.isdigit():
        return None, (jsonify({"error": _RESET_CODE_INVALID}), 400)

    # A non-string password would raise on len()/.encode(), turning a bad
    # request into a 500.
    if not isinstance(new_password, str):
        return None, (jsonify({"error": "New password is required"}), 400)

    if len(new_password) < 8:
        return None, (
            jsonify({"error": "New password must be at least 8 characters"}), 400,
        )

    return (email, code, new_password), None


def _verify_reset_code(conn, cursor, user_id: str, code: str):
    """Check a reset code for ``user_id``, returning an error response or None.

    On a wrong code the attempt counter is incremented before returning, so
    guessing is bounded by PASSWORD_RESET_MAX_ATTEMPTS.
    """
    cursor.execute(
        "SELECT password_reset_code, password_reset_code_expires_at, "
        "COALESCE(password_reset_attempts, 0) FROM users WHERE id = %s "
        # FOR UPDATE: without the row lock two concurrent requests can both read
        # the same attempt count, so the 6th guess slips past the cap and a single
        # valid code can be redeemed twice.
        "FOR UPDATE",
        (user_id,),
    )
    reset_row = cursor.fetchone()
    if not reset_row:
        return jsonify({"error": _RESET_CODE_INVALID}), 400

    stored_hash, expires_at, attempts = reset_row

    # No code was ever issued for this account.
    if not stored_hash:
        return jsonify({"error": _RESET_CODE_INVALID}), 400

    # Checked before the comparison, so guessing right on attempt 6 still fails.
    if attempts >= PASSWORD_RESET_MAX_ATTEMPTS:
        return jsonify({"error": "Too many attempts. Please request a new code."}), 429

    if expires_at and datetime.now() > expires_at:
        return jsonify({"error": _RESET_CODE_INVALID}), 400

    code_hash = hashlib.sha256(code.encode()).hexdigest()
    # compare_digest, not ==, so a wrong code can't be narrowed down
    # byte-by-byte from response timing.
    if not hmac.compare_digest(stored_hash, code_hash):
        cursor.execute(
            "UPDATE users SET password_reset_attempts = "
            "COALESCE(password_reset_attempts, 0) + 1 WHERE id = %s",
            (user_id,),
        )
        conn.commit()
        return jsonify({"error": _RESET_CODE_INVALID}), 400

    return None


@auth_bp.route('/reset-password', methods=['POST'])
# Per-email, as with /forgot-password. The hard guessing limit is the DB attempt
# counter in _verify_reset_code(); this only blunts the request rate.
@limiter.limit("10 per minute;30 per hour", key_func=get_public_auth_rate_limit_key)
def reset_password():
    """Set a new password using the code from /forgot-password.

    Body: { email, code, newPassword }. Unauthenticated — possession of the
    code proves inbox ownership. A correct code also marks the email verified,
    since delivery just demonstrated the address works.
    """
    try:
        fields, error = _parse_reset_request(request.get_json(silent=True) or {})
        if error:
            return error
        email, code, new_password = fields

        with db_pool.get_admin_connection() as conn, conn.cursor() as cursor:
            row = _find_user_for_email(cursor, email)
            if not row:
                return jsonify({"error": _RESET_CODE_INVALID}), 400

            user_id = row[0]
            code_error = _verify_reset_code(conn, cursor, user_id, code)
            if code_error:
                return code_error

            # Code verified. Clear it in the same statement that sets the
            # password so it can't be replayed, and drop must_change_password —
            # the user just chose this password.
            new_hash = bcrypt.hashpw(
                new_password.encode('utf-8'), bcrypt.gensalt()
            ).decode('utf-8')
            cursor.execute(
                "UPDATE users SET password_hash = %s, must_change_password = FALSE, "
                "email_verified = TRUE, password_reset_code = NULL, "
                "password_reset_code_expires_at = NULL, password_reset_attempts = 0 "
                "WHERE id = %s",
                (new_hash, user_id),
            )
            conn.commit()

        logging.info("Password reset completed for user %s", user_id)

        from utils.auth.stateless_auth import resolve_org_id
        record_audit_event(
            resolve_org_id(user_id) or "", user_id,
            "password_reset", "user", user_id, {}, request,
        )
        return jsonify({"message": "Password reset successfully"}), 200
    except Exception:
        logging.exception("Error in /reset-password")
        return jsonify({"error": "Password reset failed"}), 500


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
                    "SELECT u.role, u.org_id, o.name, COALESCE(u.must_change_password, FALSE), "
                    "COALESCE(u.email_verified, FALSE), (u.github_user_id IS NOT NULL) "
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
                    "emailVerified": bool(row[4]),
                    "isGithubProvisioned": bool(row[5]),
                }), 200
    except Exception:
        logging.exception("Error in /me")
        return jsonify({"error": _SERVER_ERROR}), 500


@auth_bp.route('/verify-email', methods=['POST'])
@require_auth_only
def verify_email(user_id):
    """Verify user's email with a 6-digit code."""
    data = request.get_json()
    code = (data.get('code') or '').strip() if data else ''

    if len(code) != 6 or not code.isdigit():
        return jsonify({"error": "Invalid code format"}), 400

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT email_verification_code, email_verification_code_expires_at, "
                    "email_verified, email_verification_attempts "
                    "FROM users WHERE id = %s",
                    (user_id,),
                )
                row = cursor.fetchone()
                if not row:
                    return jsonify({"error": "User not found"}), 404

                stored_code, expires_at, already_verified, attempts = row

                if already_verified:
                    return jsonify({"error": "Email already verified"}), 400
                if (attempts or 0) >= 5:
                    return jsonify({"error": "Too many attempts. Please resend a new code."}), 429
                if expires_at and datetime.now() > expires_at:
                    return jsonify({"error": "Code expired"}), 400

                code_hash = hashlib.sha256(code.encode()).hexdigest()
                if not stored_code or stored_code != code_hash:
                    cursor.execute(
                        "UPDATE users SET email_verification_attempts = "
                        "COALESCE(email_verification_attempts, 0) + 1 WHERE id = %s",
                        (user_id,),
                    )
                    conn.commit()
                    return jsonify({"error": "Invalid verification code"}), 400

                cursor.execute(
                    "UPDATE users SET email_verified = TRUE, "
                    "email_verification_code = NULL, email_verification_code_expires_at = NULL, "
                    "email_verification_attempts = 0 WHERE id = %s",
                    (user_id,),
                )
                conn.commit()

        return jsonify({"status": "success"})
    except Exception:
        logging.exception("Error in /verify-email")
        return jsonify({"error": _SERVER_ERROR}), 500


@auth_bp.route('/resend-verification', methods=['POST'])
@require_auth_only
def resend_verification(user_id):
    """Resend email verification code."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT email, email_verified, email_verification_code_expires_at "
                    "FROM users WHERE id = %s", (user_id,),
                )
                row = cursor.fetchone()
                if not row:
                    return jsonify({"error": "User not found"}), 404
                if row[1]:
                    return jsonify({"error": "Email already verified"}), 400
                if row[2]:
                    earliest_resend = row[2] - timedelta(minutes=VERIFICATION_CODE_EXPIRY_MINUTES - RESEND_COOLDOWN_MINUTES)
                    if datetime.now() < earliest_resend:
                        return jsonify({"error": "Please wait before requesting a new code"}), 429

        if not send_verification_email(user_id, row[0]):
            return jsonify({"error": "Failed to send verification email"}), 500
        return jsonify({"status": "success"})
    except Exception:
        logging.exception("Error in /resend-verification")
        return jsonify({"error": _SERVER_ERROR}), 500


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
