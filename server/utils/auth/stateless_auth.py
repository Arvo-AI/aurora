"""Stateless authentication utilities."""
import json
import logging
from typing import Optional, Dict, Any, List
from flask import request, jsonify
from utils.db.db_utils import connect_to_db_as_user
from utils.log_sanitizer import sanitize

# Configure logging
logger = logging.getLogger(__name__)


def resolve_org_id(user_id: str) -> Optional[str]:
    """Resolve org_id for a user, working both inside and outside Flask request context.

    Priority:
      1. Flask request header X-Org-ID (if in request context)
      2. flask.g cache (if in request context)
      3. DB lookup from users table (always works)

    Safe to call from Celery tasks, background threads, etc.
    """
    # Try request-context path first (fast, cached)
    try:
        org_id = get_org_id_from_request()
        if org_id:
            return org_id
    except Exception:
        logger.debug("resolve_org_id: no request context available")

    # Fallback: direct DB lookup (works outside request context)
    if not user_id:
        return None
    try:
        from utils.db.connection_pool import db_pool
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                # No RLS needed — users not RLS-protected
                cursor.execute("SELECT org_id FROM users WHERE id = %s", (user_id,))
                row = cursor.fetchone()
                if row and row[0]:
                    return row[0]
    except Exception as e:
        logger.debug("resolve_org_id: DB lookup failed for user: %s", e)

    return None

# ---------------------------------------------------------------------------
# AWS credential cache (per-process, 55-minute TTL)
# ---------------------------------------------------------------------------

_aws_cache: dict[tuple[str, str], dict] = {}
# structure: {(user_id, account_id): creds_dict}


def _get_cached_aws_creds(user_id: str, account_id: str):
    key = (user_id, account_id)
    creds = _aws_cache.get(key)
    if not creds:
        return None
    # 60-second safety margin
    if creds.get("expires_at", 0) <= __import__("time").time() + 60:
        _aws_cache.pop(key, None)
        return None
    return creds


def _put_cached_aws_creds(user_id: str, account_id: str, creds: dict):
    _aws_cache[(user_id, account_id)] = creds


def invalidate_cached_aws_creds(user_id: str, account_id: str | None = None):
    """Remove AWS creds from the in-process cache."""
    if account_id:
        _aws_cache.pop((user_id, account_id), None)
    else:
        # drop all entries for user
        for key in list(_aws_cache):
            if key[0] == user_id:
                _aws_cache.pop(key, None)


def is_valid_user_id(user_id: str) -> bool:
    """Validate that user_id is a non-empty string."""
    return bool(user_id and isinstance(user_id, str))


def validate_user_exists(user_id: str) -> bool:
    """Check that a user_id actually exists in the database.

    Use this for trust boundaries where user_id comes from an untrusted
    source (e.g. WebSocket messages) rather than the auth middleware.
    """
    if not user_id or not isinstance(user_id, str) or len(user_id) > 255:
        return False
    try:
        from utils.db.connection_pool import db_pool
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                # No RLS needed — users not RLS-protected
                cursor.execute("SELECT 1 FROM users WHERE id = %s", (user_id,))
                return cursor.fetchone() is not None
    except Exception as e:
        logger.warning("Failed to validate user_id %s: %s", sanitize(user_id), e)
        return False


def get_user_id_from_request() -> Optional[str]:
    """Extract user ID from X-User-ID header (set by Auth.js middleware).
    
    SIMPLIFIED AUTHENTICATION - Only X-User-ID header:
    All authenticated users must provide X-User-ID header from Auth.js session.
    
    Returns None if no valid authentication is present.
    """
    user_id = request.headers.get('X-User-ID')
    if user_id:
        logger.debug(f"Found authenticated user_id in header: {user_id}")
        return user_id
    
    logger.debug("No user_id found in request - user not authenticated")
    return None


def get_org_id_from_request() -> Optional[str]:
    """Extract org ID from X-Org-ID header (set by Auth.js middleware).
    
    Trusts the header value since it's set server-side by the Next.js
    middleware from the JWT session (same trust boundary as X-User-ID).
    Falls back to looking up the user's org from the database if the
    header is not present. Result is cached on flask.g for the duration
    of the request.
    
    Returns None if no org context is available.
    """
    from flask import g
    cached = getattr(g, '_org_id_resolved', None)
    if cached is not None:
        return cached if cached != '' else None

    org_id = request.headers.get('X-Org-ID')
    if org_id:
        g._org_id_resolved = org_id
        return org_id

    user_id = request.headers.get('X-User-ID')
    if user_id:
        try:
            from utils.db.connection_pool import db_pool
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cursor:
                    # No RLS needed — users not RLS-protected
                    cursor.execute(
                        "SELECT org_id FROM users WHERE id = %s",
                        (user_id,)
                    )
                    row = cursor.fetchone()
                    if row and row[0]:
                        g._org_id_resolved = row[0]
                        return row[0]
        except Exception as e:
            logger.warning(f"Error looking up org_id for user {sanitize(user_id)}: {e}")

    g._org_id_resolved = ''
    return None


def get_credentials_from_db(user_id: str, provider: str) -> Optional[Dict[str, Any]]:
    """
    Retrieve credentials from database or Vault.
    This function now automatically handles secret references and dual-session scenarios.
    """
    try:
        # --- NEW LOGIC ---
        # For AWS we no longer store token_data; we store metadata in user_connections and assume role on demand.
        if provider == 'aws':
            try:
                from utils.aws.aws_auth import assume_role_and_get_creds
                from utils.workspace.workspace_utils import get_or_create_workspace

                conn = connect_to_db_as_user()
                cur = conn.cursor()
                org_id = set_rls_context(cur, conn, user_id, log_prefix="[Creds:AWS]")

                cur.execute(
                    """
                    SELECT role_arn, account_id FROM user_connections
                    WHERE (user_id = %s OR org_id = %s) AND provider = 'aws' AND status = 'active'
                    ORDER BY CASE WHEN user_id = %s THEN 0 ELSE 1 END,
                             last_verified_at DESC NULLS LAST
                    LIMIT 1;
                    """,
                    (user_id, org_id, user_id),
                )
                row = cur.fetchone()
            finally:
                if 'cur' in locals() and cur:
                    cur.close()
                if 'conn' in locals() and conn:
                    conn.close()

            if not row:
                logger.warning(f"No active AWS connection found for user {user_id}")
                return None

            role_arn, account_id = row
            # Try cache
            cached = _get_cached_aws_creds(user_id, account_id)
            if cached:
                logger.debug("Returned cached AWS credentials for %s/%s", user_id, account_id)
                return cached

            # Get external_id from workspace (required for role assumption)
            workspace = get_or_create_workspace(user_id, "default")
            external_id = workspace.get('aws_external_id')
            if not external_id:
                logger.error(f"Workspace for user {user_id} missing aws_external_id - cannot assume role")
                return None

            try:
                creds, _ = assume_role_and_get_creds(role_arn, external_id=external_id)
                logger.info(f"Assumed role for user {user_id} (AWS account {account_id})")
                _put_cached_aws_creds(user_id, account_id, creds)
                return creds
            except Exception as e:
                logger.error(f"Error assuming role for user {user_id}: {e}")
                return None

        # Non-AWS providers continue to use Vault via secret_ref_utils
        from utils.secrets.secret_ref_utils import get_user_token_data
        token_data = get_user_token_data(user_id, provider)
        
        if token_data:
            # For Azure, add subscription info if available
            if provider == 'azure':
                # Get subscription info from database if needed
                try:
                    conn = connect_to_db_as_user()
                    cursor = conn.cursor()
                    org_id = set_rls_context(cursor, conn, user_id, log_prefix="[Creds:Azure]")
                    
                    cursor.execute(
                        """SELECT subscription_id, subscription_name
                           FROM user_tokens
                           WHERE (user_id = %s OR org_id = %s) AND provider = %s
                           ORDER BY CASE WHEN user_id = %s THEN 0 ELSE 1 END, timestamp DESC
                           LIMIT 1""",
                        (user_id, org_id, provider, user_id)
                    )
                    result = cursor.fetchone()
                    
                    if result:
                        subscription_id, subscription_name = result
                        if subscription_id:
                            token_data['subscription_id'] = subscription_id
                            token_data['subscription_name'] = subscription_name
                            
                except Exception as e:
                    logger.warning(f"Failed to get Azure subscription info for user {user_id}: {e}")
                finally:
                    if 'cursor' in locals() and cursor:
                        cursor.close()
                    if 'conn' in locals() and conn:
                        conn.close()
            
            logger.info(f"Retrieved {provider} credentials for user {user_id}")
            return token_data
        
        logger.warning(f"No {provider} credentials found for user {user_id}")
        return None
        
    except Exception as e:
        logger.error(f"Error retrieving credentials for {user_id}/{provider}: {e}")
        return None

def store_deployment_task(user_id: str, task_id: str, deployment_id: str = None, status: str = "started", task_data: Dict = None):
    """Store deployment task in database instead of session."""
    try:
        conn = connect_to_db_as_user()
        cursor = conn.cursor()
        set_rls_context(cursor, conn, user_id, log_prefix="[StoreDeployTask]")
        
        cursor.execute("""
            INSERT INTO deployment_tasks (user_id, task_id, deployment_id, status, task_data)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (user_id, task_id) DO UPDATE SET
                deployment_id = EXCLUDED.deployment_id,
                status = EXCLUDED.status,
                task_data = EXCLUDED.task_data,
                updated_at = CURRENT_TIMESTAMP
        """, (user_id, task_id, deployment_id, status, json.dumps(task_data) if task_data else None))
        conn.commit()
        logger.info(f"Stored deployment task {task_id} for user {user_id}")
    except Exception as e:
        logger.error(f"Error storing deployment task: {e}")
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if 'conn' in locals() and conn:
            conn.close()

def get_deployment_task(user_id: str, task_id: str = None) -> Optional[Dict]:
    """Get deployment task from database."""
    try:
        conn = connect_to_db_as_user()
        cursor = conn.cursor()
        set_rls_context(cursor, conn, user_id, log_prefix="[GetDeployTask]")
        
        if task_id:
            cursor.execute(
                "SELECT task_id, deployment_id, status, task_data FROM deployment_tasks WHERE user_id = %s AND task_id = %s",
                (user_id, task_id)
            )
        else:
            cursor.execute(
                "SELECT task_id, deployment_id, status, task_data FROM deployment_tasks WHERE user_id = %s ORDER BY updated_at DESC LIMIT 1",
                (user_id,)
            )
        
        result = cursor.fetchone()
        if result:
            task_id, deployment_id, status, task_data = result
            logger.info(f"Retrieved deployment task {task_id} for user {user_id}")
            return {
                'task_id': task_id,
                'deployment_id': deployment_id,
                'status': status,
                'task_data': json.loads(task_data) if task_data else {}
            }
        
        logger.warning(f"No deployment task found for user {user_id}")
        return None
    except Exception as e:
        logger.error(f"Error retrieving deployment task: {e}")
        return None
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if 'conn' in locals() and conn:
            conn.close()

def store_user_preference(user_id: str, key: str, value: Any):
    """Store a user-scoped preference.

    For org-scoped preferences (shared across an organization), use
    store_org_preference() instead — passing an "__org__<uuid>" pseudo-user id
    here will fail because that id does not exist in the users table.
    """
    try:
        conn = connect_to_db_as_user()
        cursor = conn.cursor()
        org_id = set_rls_context(cursor, conn, user_id, log_prefix="[StoreUserPref]")
        if not org_id:
            logger.error(
                "store_user_preference: cannot resolve org for user %s; "
                "preference %s not written. Use store_org_preference() for org-scoped keys.",
                sanitize(user_id), sanitize(key),
            )
            return

        cursor.execute(
            "DELETE FROM user_preferences WHERE org_id = %s AND preference_key = %s",
            (org_id, key),
        )
        cursor.execute("""
            INSERT INTO user_preferences (user_id, org_id, preference_key, preference_value)
            VALUES (%s, %s, %s, %s)
        """, (user_id, org_id, key, json.dumps(value)))
        conn.commit()
        logger.debug("Stored org preference successfully")
    except Exception:
        logger.exception("Error storing user preference")
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if 'conn' in locals() and conn:
            conn.close()


_ORG_PREF_UPSERT_SQL = (
    "INSERT INTO user_preferences (user_id, org_id, preference_key, preference_value) "
    "VALUES (%s, %s, %s, %s) "
    "ON CONFLICT (user_id, org_id, preference_key) WHERE org_id IS NOT NULL DO UPDATE "
    "SET preference_value = EXCLUDED.preference_value, updated_at = CURRENT_TIMESTAMP"
)


def _org_pseudo_user_id(org_id: str) -> str:
    return f"__org__{org_id}"


def _set_org_rls(cursor, org_id: str) -> None:
    """Set RLS session vars directly from an org_id (no users-table lookup).

    Needed because org-scoped prefs use a synthetic "__org__<uuid>" user id
    that set_rls_context() can't resolve via get_org_id_for_user().
    """
    cursor.execute("SET myapp.current_user_id = %s;", (_org_pseudo_user_id(org_id),))
    cursor.execute("SET myapp.current_org_id = %s;", (org_id,))


def store_org_preference(org_id: str, key: str, value: Any, *, cursor=None) -> None:
    """Upsert an org-scoped preference row.

    Org-scoped preferences are stored with a synthetic user_id of
    "__org__<org_id>" so they share the user_preferences table without
    colliding with real user-scoped keys. When `cursor` is provided, the
    caller is responsible for RLS context and commit; otherwise an admin
    connection is opened here and RLS is configured from org_id directly.
    """
    if not org_id:
        raise ValueError("store_org_preference requires a non-empty org_id")

    params = (_org_pseudo_user_id(org_id), org_id, key, json.dumps(value))

    if cursor is not None:
        cursor.execute(_ORG_PREF_UPSERT_SQL, params)
        return

    from utils.db.connection_pool import db_pool
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                _set_org_rls(cur, org_id)
                cur.execute(_ORG_PREF_UPSERT_SQL, params)
            conn.commit()
    except Exception:
        logger.exception("Error storing org preference %s for org %s", sanitize(key), sanitize(org_id))


def get_org_preference(org_id: str, key: str, default=None):
    """Read an org-scoped preference written via store_org_preference().

    Uses an admin connection with RLS configured from org_id directly, so it
    works outside Flask request context (e.g. Celery) where connection-pool
    RLS vars aren't auto-populated.
    """
    if not org_id:
        raise ValueError("get_org_preference requires a non-empty org_id")

    from utils.db.connection_pool import db_pool
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                _set_org_rls(cur, org_id)
                cur.execute(
                    "SELECT preference_value FROM user_preferences "
                    "WHERE org_id = %s AND user_id = %s AND preference_key = %s",
                    (org_id, _org_pseudo_user_id(org_id), key),
                )
                row = cur.fetchone()
                if not row:
                    return default
                return _parse_preference_value(row[0], default)
    except Exception:
        logger.exception("Error reading org preference %s for org %s", sanitize(key), sanitize(org_id))
        return default

def _parse_preference_value(raw, default=None):
    """Decode a preference_value column, which may be JSON text or already decoded."""
    if raw is None:
        return default
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw

def get_user_preference(user_id: str, key: str, default=None):
    """Get user preference from database."""
    conn = None
    cursor = None
    try:
        conn = connect_to_db_as_user()
        cursor = conn.cursor()
        org_id = set_rls_context(cursor, conn, user_id, log_prefix="[Prefs:get]")

        lookup_col, lookup_val = ("org_id", org_id) if org_id else ("user_id", user_id)
        cursor.execute(
            f"SELECT preference_value FROM user_preferences WHERE {lookup_col} = %s AND preference_key = %s",
            (lookup_val, key),
        )
        result = cursor.fetchone()
        if not result:
            return default
        return _parse_preference_value(result[0], default)
    except Exception:
        logger.exception("Error retrieving user preference")
        return default
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def get_connected_providers(user_id: str) -> List[str]:
    """Get list of connected cloud providers for a user from database.
    
    Checks both user_tokens (OAuth/secret-based) and user_connections (role-based)
    to determine which providers are actually connected.
    Includes org-shared connections so all org members see the same providers.
    
    Args:
        user_id: The user ID to check
        
    Returns:
        List of connected provider IDs (e.g., ['gcp', 'aws', 'azure'])
    """
    if not user_id:
        return []
    
    org_id = resolve_org_id(user_id)
    connected_providers = []
    
    try:
        conn = connect_to_db_as_user()
        cursor = conn.cursor()
        set_rls_context(cursor, conn, user_id, log_prefix="[ConnectedProviders]")
        
        # Check user_tokens table (OAuth/secret-based providers)
        cursor.execute(
            """
            SELECT DISTINCT provider
            FROM user_tokens 
            WHERE (user_id = %s OR org_id = %s)
              AND secret_ref IS NOT NULL AND is_active = TRUE
            """,
            (user_id, org_id)
        )
        token_providers = [row[0] for row in cursor.fetchall()]
        connected_providers.extend(token_providers)
        
        # Check user_connections table (role-based connections like AWS)
        cursor.execute(
            """
            SELECT DISTINCT provider
            FROM user_connections
            WHERE (user_id = %s OR org_id = %s) AND status = 'active'
            """,
            (user_id, org_id)
        )
        connection_providers = [row[0] for row in cursor.fetchall()]
        connected_providers.extend(connection_providers)
        
        cursor.close()
        conn.close()
        
        # Remove duplicates and return sorted list
        unique_providers = sorted(list(set(connected_providers)))
        logger.debug(f"Found connected providers for user {user_id}: {unique_providers}")
        return unique_providers
        
    except Exception as e:
        logger.error(f"Error getting connected providers for user {user_id}: {e}")
        return []


def get_user_email(user_id: str) -> Optional[str]:
    """Get user email from Auth.js or database.
    
    Args:
        user_id: The Auth.js user ID
        
    Returns:
        User email address or None if not found
    """
    import os
    from utils.db.connection_pool import db_pool
    
    try:
        # Try to get email from user_tokens table first (faster)
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                # No RLS needed — user_tokens not RLS-protected
                cursor.execute(
                    "SELECT email FROM user_tokens WHERE user_id = %s AND email IS NOT NULL LIMIT 1",
                    (user_id,)
                )
                result = cursor.fetchone()
                if result and result[0]:
                    return result[0]
        
        # Auth.js doesn't have a separate API - email should be in database
        # If not found, return None
        logger.warning(f"Could not find email for user {user_id}")
        return None
        
    except Exception as e:
        logger.error(f"Error getting user email: {e}")
        return None



import time as _time

# Cache for user_id -> org_id mapping used by background tasks (with TTL)
_user_org_cache: dict[str, tuple[str | None, float]] = {}
_USER_ORG_CACHE_TTL = 300  # 5 minutes


def get_org_id_for_user(user_id: str) -> Optional[str]:
    """Look up org_id for a user by querying the DB. Cached in-memory for task batches.

    Use this in Celery tasks where there is no Flask request context.
    """
    entry = _user_org_cache.get(user_id)
    if entry is not None:
        cached_org_id, cached_at = entry
        if _time.monotonic() - cached_at < _USER_ORG_CACHE_TTL:
            return cached_org_id

    try:
        from utils.db.connection_pool import db_pool
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                # No RLS needed — users not RLS-protected
                cursor.execute("SELECT org_id FROM users WHERE id = %s", (user_id,))
                row = cursor.fetchone()
                org_id = row[0] if row and row[0] else None
                _user_org_cache[user_id] = (org_id, _time.monotonic())
                return org_id
    except Exception as e:
        logger.warning("Error looking up org_id for user %s: %s", sanitize(user_id), type(e).__name__)
        return None


def set_rls_context(cursor, conn, user_id: str, *, log_prefix: str = "") -> Optional[str]:
    """Resolve org_id and configure RLS session variables on a DB connection.

    Returns the org_id on success, or None (and logs an error) when the org
    cannot be resolved — callers should abort persistence in that case.
    """
    org_id = get_org_id_for_user(user_id)
    if not org_id:
        logger.error("%s Missing org_id for user %s; cannot set RLS context", log_prefix, sanitize(user_id))
        return None

    cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
    cursor.execute("SET myapp.current_org_id = %s;", (org_id,))
    conn.commit()
    return org_id