# utils/connection_utils.py
"""Utility helpers for working with the user_connections table."""

import logging
from datetime import datetime
from typing import Optional, List, Dict

from utils.db.db_utils import connect_to_db_as_user, connect_to_db_as_admin

logger = logging.getLogger(__name__)


def save_connection_metadata(
    user_id: str,
    provider: str,
    account_id: str,
    *,
    role_arn: Optional[str] = None,
    read_only_role_arn: Optional[str] = None,
    connection_method: Optional[str] = None,
    region: Optional[str] = None,
    status: str = "active",
) -> bool:
    """Insert or update a row in user_connections.

    Uses an UPSERT so callers can invoke freely.
    Returns True on success, False otherwise.
    """
    sql = """
        INSERT INTO user_connections (
            user_id, provider, account_id, role_arn, read_only_role_arn,
            connection_method, region, status, last_verified_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (user_id, provider, account_id)
        DO UPDATE SET
            role_arn = EXCLUDED.role_arn,
            read_only_role_arn = EXCLUDED.read_only_role_arn,
            connection_method = EXCLUDED.connection_method,
            region = COALESCE(EXCLUDED.region, user_connections.region),
            status = EXCLUDED.status,
            last_verified_at = EXCLUDED.last_verified_at;
    """
    conn = None
    try:
        conn = connect_to_db_as_admin()
        logger.info(
            "[CONN-META] Saving connection user=%s provider=%s account=%s status=%s",
            user_id,
            provider,
            account_id,
            status,
        )
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    user_id,
                    provider,
                    account_id,
                    role_arn,
                    read_only_role_arn,
                    connection_method,
                    region,
                    status,
                    datetime.utcnow(),
                ),
            )
        conn.commit()
        logger.info("[CONN-META] Upsert successful for %s/%s/%s", user_id, provider, account_id)
        return True
    except Exception as e:
        logger.error("Failed to save connection metadata: %s", e)
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            conn.close()


def set_connection_status(
    user_id: str,
    provider: str,
    account_id: str,
    status: str,
) -> bool:
    """Update the status column for a connection (disconnect etc.)."""
    sql = """
        UPDATE user_connections
        SET status = %s, last_verified_at = %s
        WHERE user_id = %s AND provider = %s AND account_id = %s;
    """
    conn = None
    try:
        conn = connect_to_db_as_admin()
        logger.info(
            "[CONN-META] Updating status user=%s provider=%s account=%s → %s",
            user_id,
            provider,
            account_id,
            status,
        )
        with conn.cursor() as cur:
            cur.execute(sql, (status, datetime.utcnow(), user_id, provider, account_id))
        conn.commit()
        logger.info("[CONN-META] Status update success for %s/%s/%s", user_id, provider, account_id)
        return True
    except Exception as e:
        logger.error("Failed to set connection status: %s", e)
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            conn.close()


def list_active_connections(user_id: str) -> List[Dict]:
    """Return active connections for a user as list of dicts."""
    sql = """
        SELECT provider, account_id, connection_method, role_arn, read_only_role_arn, region, last_verified_at
        FROM user_connections
        WHERE user_id = %s AND status = 'active';
    """
    conn = None
    try:
        conn = connect_to_db_as_user()
        with conn.cursor() as cur:
            cur.execute("SET myapp.current_user_id = %s;", (user_id,))
            conn.commit()
            cur.execute(sql, (user_id,))
            rows = cur.fetchall()
        logger.info("[CONN-META] Fetched %d active connections for user %s", len(rows), user_id)
        return [
            {
                "provider": r[0],
                "account_id": r[1],
                "connection_method": r[2],
                "role_arn": r[3],
                "read_only_role_arn": r[4],
                "region": r[5],
                "last_verified_at": r[6].isoformat() if r[6] else None,
            }
            for r in rows
        ]
    except Exception as e:
        logger.error("Error listing active connections: %s", e)
        return []
    finally:
        if conn:
            conn.close()


def get_user_aws_connection(user_id: str) -> Optional[Dict]:
    """Get the first active AWS connection for a user from user_connections table.
    
    This is the single source of truth for AWS connections.
    Returns None if no active AWS connection exists.
    For multi-account users, use get_all_user_aws_connections() instead.
    """
    sql = """
        SELECT account_id, role_arn, read_only_role_arn, connection_method, region, last_verified_at
        FROM user_connections
        WHERE user_id = %s AND provider = 'aws' AND status = 'active'
        LIMIT 1;
    """
    conn = None
    try:
        conn = connect_to_db_as_user()
        with conn.cursor() as cur:
            cur.execute("SET myapp.current_user_id = %s;", (user_id,))
            conn.commit()
            cur.execute(sql, (user_id,))
            row = cur.fetchone()
            
            if row:
                return {
                    "account_id": row[0],
                    "role_arn": row[1],
                    "read_only_role_arn": row[2],
                    "connection_method": row[3],
                    "region": row[4],
                    "last_verified_at": row[5].isoformat() if row[5] else None,
                }
            return None
    except Exception as e:
        logger.error("Error getting AWS connection for user %s: %s", user_id, e)
        return None
    finally:
        if conn:
            conn.close()


def get_all_user_aws_connections(user_id: str) -> List[Dict]:
    """Get all active AWS connections for a user.

    Returns a list of connection dicts, one per connected AWS account.
    Each dict includes account_id, role_arn, read_only_role_arn, region,
    connection_method, and last_verified_at.
    """
    sql = """
        SELECT account_id, role_arn, read_only_role_arn, connection_method, region, last_verified_at
        FROM user_connections
        WHERE user_id = %s AND provider = 'aws' AND status = 'active'
        ORDER BY account_id;
    """
    conn = None
    try:
        conn = connect_to_db_as_user()
        with conn.cursor() as cur:
            cur.execute("SET myapp.current_user_id = %s;", (user_id,))
            conn.commit()
            cur.execute(sql, (user_id,))
            rows = cur.fetchall()

        logger.info("[CONN-META] Fetched %d active AWS connections for user %s", len(rows), user_id)
        return [
            {
                "account_id": row[0],
                "role_arn": row[1],
                "read_only_role_arn": row[2],
                "connection_method": row[3],
                "region": row[4],
                "last_verified_at": row[5].isoformat() if row[5] else None,
            }
            for row in rows
        ]
    except Exception as e:
        logger.error("Error getting AWS connections for user %s: %s", user_id, e)
        return []
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------------
# AWS-specific helpers
# ---------------------------------------------------------------------------


def extract_account_id_from_arn(role_arn: str) -> Optional[str]:
    """Return the 12-digit AWS account ID from an IAM Role ARN.

    Examples
    --------
    >>> extract_account_id_from_arn("arn:aws:iam::123456789012:role/MyRole")
    '123456789012'
    """
    try:
        parts = role_arn.split(":")
        if len(parts) < 5:
            return None
        return parts[4] or None  # 4th index is account id for standard ARNs
    except Exception:
        return None

# ---------------------------------------------------------------------------
# Secrets cleanup helpers
# ---------------------------------------------------------------------------


def delete_connection_secret(
    user_id: str,
    provider: str,
    account_id: str,
) -> bool:
    """Mark a user connection as inactive in user_connections table.

    For providers that use Vault secrets (GCP, Azure, etc.), this also deletes
    the Vault secret. For providers using STS AssumeRole (AWS), it just marks
    the connection inactive.

    Returns ``True`` when database update succeeds.
    """

    # Check if connection exists and get secret_ref if present
    sql_select = (
        "SELECT role_arn "
        "FROM user_connections "
        "WHERE user_id = %s AND provider = %s AND account_id = %s AND status = 'active' LIMIT 1;"
    )

    # Mark connection as inactive
    sql_update = (
        "UPDATE user_connections "
        "SET status = 'inactive', last_verified_at = %s "
        "WHERE user_id = %s AND provider = %s AND account_id = %s;"
    )

    conn = None
    try:
        conn = connect_to_db_as_admin()
        with conn.cursor() as cur:
            # Check if connection exists
            cur.execute(sql_select, (user_id, provider, account_id))
            row = cur.fetchone()
            
            if not row:
                logger.warning("[CONN-META] No active connection found for %s/%s/%s", user_id, provider, account_id)
                return False

            # For providers that might have Vault secrets, try to delete them
            # (Note: AWS uses STS AssumeRole, so no Vault secrets to delete)
            if provider in ['gcp', 'azure', 'github']:
                try:
                    from utils.secrets.secret_ref_utils import SecretRefManager
                    # Try to get secret_ref if column exists (may not for all schemas)
                    try:
                        cur.execute(
                            "SELECT secret_ref FROM user_connections WHERE user_id = %s AND provider = %s AND account_id = %s",
                            (user_id, provider, account_id)
                        )
                        secret_row = cur.fetchone()
                        if secret_row and secret_row[0]:
                            srm = SecretRefManager()
                            srm.delete_secret(secret_row[0])
                    except Exception:
                        # Column doesn't exist or no secret_ref - that's fine
                        pass
                except Exception as e:
                    logger.warning("[CONN-META] Vault secret deletion skipped for %s/%s/%s: %s", user_id, provider, account_id, e)

            # Mark connection inactive
            cur.execute(
                sql_update,
                (
                    datetime.utcnow(),
                    user_id,
                    provider,
                    account_id,
                ),
            )

        conn.commit()
        logger.info(
            "[CONN-META] Connection %s/%s/%s marked as inactive",
            user_id,
            provider,
            account_id,
        )
        return True
    except Exception as e:
        logger.error("[CONN-META] Failed to delete connection: %s", e)
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            conn.close()
