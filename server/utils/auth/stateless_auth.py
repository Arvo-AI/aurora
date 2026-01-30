"""Stateless authentication utilities."""
import json
import logging
from typing import Optional, Dict, Any, List
from flask import request, jsonify
from utils.db.db_utils import connect_to_db_as_user

# Configure logging
logger = logging.getLogger(__name__)

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
                cur.execute("SET myapp.current_user_id = %s;", (user_id,))
                conn.commit()

                cur.execute(
                    """
                    SELECT role_arn, account_id FROM user_connections
                    WHERE user_id = %s AND provider = 'aws' AND status = 'active'
                    ORDER BY last_verified_at DESC NULLS LAST LIMIT 1;
                    """,
                    (user_id,),
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
                    cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
                    conn.commit()
                    
                    cursor.execute(
                        "SELECT subscription_id, subscription_name FROM user_tokens WHERE user_id = %s AND provider = %s ORDER BY timestamp DESC LIMIT 1",
                        (user_id, provider)
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
        cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
        conn.commit()
        
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
        cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
        conn.commit()
        
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
    """Store user preference in database."""
    try:
        conn = connect_to_db_as_user()
        cursor = conn.cursor()
        cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
        conn.commit()
        
        cursor.execute("""
            INSERT INTO user_preferences (user_id, preference_key, preference_value)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id, preference_key) DO UPDATE SET
                preference_value = EXCLUDED.preference_value,
                updated_at = CURRENT_TIMESTAMP
        """, (user_id, key, json.dumps(value)))
        conn.commit()
        logger.debug(f"Stored preference {key} for user {user_id}")
    except Exception as e:
        logger.error(f"Error storing user preference: {e}")
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if 'conn' in locals() and conn:
            conn.close()

def get_user_preference(user_id: str, key: str, default=None):
    """Get user preference from database."""
    try:
        conn = connect_to_db_as_user()
        cursor = conn.cursor()
        cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
        conn.commit()
        
        cursor.execute(
            "SELECT preference_value FROM user_preferences WHERE user_id = %s AND preference_key = %s",
            (user_id, key)
        )
        result = cursor.fetchone()
        if result:
            logger.debug(f"Retrieved preference {key} for user {user_id}")
            try:
                # Try to parse as JSON, but handle cases where it might already be decoded
                # Use 'is not None' to handle boolean False values correctly
                value = result[0] if result[0] is not None else default
                if isinstance(value, str):
                    return json.loads(value)
                return value
            except json.JSONDecodeError:
                # If JSON parsing fails, return the raw value
                return result[0] if result[0] is not None else default
        
        logger.debug(f"No preference {key} found for user {user_id}, returning default")
        return default
    except Exception as e:
        logger.error(f"Error retrieving user preference: {e}")
        return default
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if 'conn' in locals() and conn:
            conn.close()

def get_connected_providers(user_id: str) -> List[str]:
    """Get list of connected cloud providers for a user from database.
    
    Checks both user_tokens (OAuth/secret-based) and user_connections (role-based)
    to determine which providers are actually connected.
    
    Args:
        user_id: The user ID to check
        
    Returns:
        List of connected provider IDs (e.g., ['gcp', 'aws', 'azure'])
    """
    if not user_id:
        return []
    
    connected_providers = []
    
    try:
        conn = connect_to_db_as_user()
        cursor = conn.cursor()
        cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
        conn.commit()
        
        # Check user_tokens table (OAuth/secret-based providers)
        cursor.execute(
            """
            SELECT DISTINCT provider
            FROM user_tokens 
            WHERE user_id = %s AND secret_ref IS NOT NULL AND is_active = TRUE
            """,
            (user_id,)
        )
        token_providers = [row[0] for row in cursor.fetchall()]
        connected_providers.extend(token_providers)
        
        # Check user_connections table (role-based connections like AWS)
        cursor.execute(
            """
            SELECT DISTINCT provider
            FROM user_connections
            WHERE user_id = %s AND status = 'active'
            """,
            (user_id,)
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

def create_cors_response():
    """Create a CORS response for OPTIONS requests."""
    from flask import make_response
    response = make_response()
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-User-ID'
    return response 