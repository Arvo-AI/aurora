# utils/connection_utils.py
"""Utility helpers for working with the user_connections table."""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, List, Dict, Tuple

from utils.db.db_utils import connect_to_db_as_user, connect_to_db_as_admin
from utils.auth.stateless_auth import set_rls_context
from utils.log_sanitizer import sanitize, safe_provider, hash_for_log

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GcpConnectionExtras:
    """Per-SA fields used only by GCP multi-SA rows.

    Bundled so ``save_connection_metadata`` stays under the 13-parameter cap —
    AWS callers omit the argument entirely.
    """
    account_alias: Optional[str] = None
    project_id: Optional[str] = None
    accessible_project_ids: Optional[List[str]] = None
    visibility: Optional[str] = None
    secret_ref: Optional[str] = None

# Shared SQL fragments used by multiple GCP-helper queries.
_WHERE_USER_ONLY = "user_id = %s"
_WHERE_USER_OR_ORG_SHARED = "(user_id = %s OR (org_id = %s AND visibility = 'org'))"


def _resolve_org_id(user_id: str) -> Optional[str]:
    """Resolve org_id for org-aware queries."""
    try:
        from utils.auth.stateless_auth import resolve_org_id
        org = resolve_org_id(user_id)
        if not org:
            logger.warning("[CONN-META] Could not resolve org_id — RLS context will not be set")
        return org
    except Exception as e:
        logger.warning("[CONN-META] Failed to resolve org_id: %s — RLS context will not be set", type(e).__name__)
        return None


def save_connection_metadata(
    user_id: str,
    provider: str,
    account_id: str,
    *,
    role_arn: Optional[str] = None,
    read_only_role_arn: Optional[str] = None,
    connection_method: Optional[str] = None,
    region: Optional[str] = None,
    workspace_id: Optional[str] = None,
    status: str = "active",
    gcp_extras: Optional[GcpConnectionExtras] = None,
) -> bool:
    """Insert or update a row in user_connections.

    Uses an UPSERT so callers can invoke freely. ``gcp_extras`` carries the
    multi-SA-only fields (alias / project / accessible_projects / visibility /
    secret_ref); COALESCE preserves existing values when omitted (e.g. AWS).

    Returns True on success, False otherwise.
    """
    org_id = _resolve_org_id(user_id)
    extras = gcp_extras or GcpConnectionExtras()
    accessible_json = (
        json.dumps(extras.accessible_project_ids)
        if extras.accessible_project_ids is not None
        else None
    )
    sql = """
        INSERT INTO user_connections (
            user_id, org_id, provider, account_id, role_arn, read_only_role_arn,
            connection_method, region, workspace_id, status, last_verified_at,
            account_alias, project_id, accessible_project_ids, visibility, secret_ref
        ) VALUES (
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s::jsonb, COALESCE(%s, 'private'), %s
        )
        ON CONFLICT (user_id, provider, account_id)
        DO UPDATE SET
            org_id = COALESCE(EXCLUDED.org_id, user_connections.org_id),
            role_arn = EXCLUDED.role_arn,
            read_only_role_arn = EXCLUDED.read_only_role_arn,
            connection_method = EXCLUDED.connection_method,
            region = COALESCE(EXCLUDED.region, user_connections.region),
            workspace_id = COALESCE(EXCLUDED.workspace_id, user_connections.workspace_id),
            status = EXCLUDED.status,
            last_verified_at = EXCLUDED.last_verified_at,
            account_alias = COALESCE(EXCLUDED.account_alias, user_connections.account_alias),
            project_id = COALESCE(EXCLUDED.project_id, user_connections.project_id),
            accessible_project_ids = COALESCE(EXCLUDED.accessible_project_ids, user_connections.accessible_project_ids),
            visibility = COALESCE(EXCLUDED.visibility, user_connections.visibility),
            secret_ref = COALESCE(EXCLUDED.secret_ref, user_connections.secret_ref);
    """
    conn = None
    try:
        conn = connect_to_db_as_admin()
        with conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[CONN-META:save]")
            cur.execute(
                sql,
                (
                    user_id,
                    org_id,
                    provider,
                    account_id,
                    role_arn,
                    read_only_role_arn,
                    connection_method,
                    region,
                    workspace_id,
                    status,
                    datetime.now(timezone.utc),
                    extras.account_alias,
                    extras.project_id,
                    accessible_json,
                    extras.visibility,
                    extras.secret_ref,
                ),
            )
        conn.commit()
        logger.info("[CONN-META] Upsert successful user=%s provider=%s account=%s", hash_for_log(user_id), safe_provider(provider), hash_for_log(account_id))
        return True
    except Exception:
        logger.exception("Failed to save connection metadata")
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
            hash_for_log(user_id),
            safe_provider(provider),
            hash_for_log(account_id),
            sanitize(status),
        )
        with conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[CONN-META:setStatus]")
            cur.execute(sql, (status, datetime.now(timezone.utc), user_id, provider, account_id))
        conn.commit()
        logger.info("[CONN-META] Status update success user=%s provider=%s account=%s", hash_for_log(user_id), safe_provider(provider), hash_for_log(account_id))
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
    """Return active connections for a user (including org-shared connections)."""
    org_id = _resolve_org_id(user_id)
    sql = """
        SELECT provider, account_id, connection_method, role_arn, read_only_role_arn, region, last_verified_at
        FROM user_connections
        WHERE (user_id = %s OR org_id = %s) AND status = 'active'
        ORDER BY CASE WHEN user_id = %s THEN 0 ELSE 1 END;
    """
    conn = None
    try:
        conn = connect_to_db_as_user()
        with conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[CONN-META:list]")
            cur.execute(sql, (user_id, org_id, user_id))
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
    org_id = _resolve_org_id(user_id)
    sql = """
        SELECT account_id, role_arn, read_only_role_arn, connection_method, region, last_verified_at
        FROM user_connections
        WHERE (user_id = %s OR org_id = %s) AND provider = 'aws' AND status = 'active'
        ORDER BY CASE WHEN user_id = %s THEN 0 ELSE 1 END
        LIMIT 1;
    """
    conn = None
    try:
        conn = connect_to_db_as_user()
        with conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[CONN-META:awsConn]")
            cur.execute(sql, (user_id, org_id, user_id))
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
    """Get all active AWS connections for a user (including org-shared).

    Returns a list of connection dicts, one per connected AWS account.
    Each dict includes account_id, role_arn, read_only_role_arn, region,
    connection_method, and last_verified_at.
    """
    org_id = _resolve_org_id(user_id)
    sql = """
        SELECT account_id, role_arn, read_only_role_arn, connection_method, region, last_verified_at
        FROM user_connections
        WHERE (user_id = %s OR org_id = %s) AND provider = 'aws' AND status = 'active'
        ORDER BY CASE WHEN user_id = %s THEN 0 ELSE 1 END, account_id;
    """
    conn = None
    try:
        conn = connect_to_db_as_user()
        with conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[CONN-META:allAws]")
            cur.execute(sql, (user_id, org_id, user_id))
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

    sql_select = (
        "SELECT role_arn "
        "FROM user_connections "
        "WHERE user_id = %s AND provider = %s AND account_id = %s AND status = 'active' LIMIT 1;"
    )

    sql_update = (
        "UPDATE user_connections "
        "SET status = 'inactive', last_verified_at = %s "
        "WHERE user_id = %s AND provider = %s AND account_id = %s;"
    )

    conn = None
    try:
        conn = connect_to_db_as_admin()
        with conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[CONN-META:deleteSecret]")
            cur.execute(sql_select, (user_id, provider, account_id))
            row = cur.fetchone()
            
            if not row:
                logger.warning("[CONN-META] No active connection found for user=%s provider=%s account=%s", hash_for_log(user_id), safe_provider(provider), hash_for_log(account_id))
                return False

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
                    logger.warning("[CONN-META] Vault secret deletion skipped for user=%s provider=%s account=%s: %s", hash_for_log(user_id), safe_provider(provider), hash_for_log(account_id), e)

            cur.execute(
                sql_update,
                (
                    datetime.now(timezone.utc),
                    user_id,
                    provider,
                    account_id,
                ),
            )

        conn.commit()
        logger.info(
            "[CONN-META] Connection user=%s provider=%s account=%s marked as inactive",
            hash_for_log(user_id),
            safe_provider(provider),
            hash_for_log(account_id),
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


# ---------------------------------------------------------------------------
# GCP multi-service-account helpers
#
# These intentionally live alongside the AWS helpers above (which they do not
# modify). GCP rows are one-per-(user_id, provider='gcp', account_id=sa_email),
# with project_id / accessible_project_ids / visibility / secret_ref columns.
# ---------------------------------------------------------------------------


def _gcp_visibility_clause(user_id: str) -> Tuple[str, Tuple]:
    """Build the SQL WHERE clause + params for GCP visibility scoping.

    Returns ``(where_fragment, params_tuple)`` — own private rows always
    visible, plus org-shared rows in the same org when ``org_id`` resolves.
    """
    org_id = _resolve_org_id(user_id)
    if org_id is None:
        return _WHERE_USER_ONLY, (user_id,)
    return _WHERE_USER_OR_ORG_SHARED, (user_id, org_id)


def _normalize_accessible_projects(raw, account_id_for_log: str) -> List[str]:
    """Coerce ``accessible_project_ids`` (str|list[str|dict]|other) to ``List[str]``.

    Tolerates the legacy ``[{"project_id": "foo"}]`` shape and JSON-encoded
    strings. Silently dropping malformed values would surface to the LLM as
    "this SA has zero accessible projects" — a confusing symptom of a data
    issue, so we log a warning.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception as _exc:
            logger.warning(
                "[CONN-META] Failed to decode accessible_project_ids JSON for account=%s: %s",
                hash_for_log(account_id_for_log),
                _exc,
            )
            return []
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for item in raw:
        if isinstance(item, str) and item:
            out.append(item)
        elif isinstance(item, dict):
            pid = item.get("project_id") or item.get("projectId")
            if pid:
                out.append(pid)
    return out


def _row_to_connection_dict(row) -> Dict:
    """Decode a SELECT row from the standard GCP-helper column list."""
    return {
        "account_id": row[0],
        "account_alias": row[1],
        "project_id": row[2],
        "accessible_project_ids": _normalize_accessible_projects(row[3], row[0] or ""),
        "visibility": row[4],
        "secret_ref": row[5],
        "status": row[6],
        "last_verified_at": row[7].isoformat() if row[7] else None,
    }


_GCP_SELECT_COLUMNS = (
    "account_id, account_alias, project_id, accessible_project_ids, "
    "visibility, secret_ref, status, last_verified_at"
)


def get_all_user_connections(user_id: str, provider: str) -> List[Dict]:
    """Return all active connections for (user_id, provider).

    Includes org-shared rows when ``visibility = 'org'``; private rows from
    other users in the same org are excluded.
    """
    where, params = _gcp_visibility_clause(user_id)

    sql = f"""
        SELECT {_GCP_SELECT_COLUMNS}
        FROM user_connections
        WHERE {where} AND provider = %s AND status = 'active'
        ORDER BY CASE WHEN user_id = %s THEN 0 ELSE 1 END, account_id;
    """
    conn = None
    try:
        conn = connect_to_db_as_user()
        with conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[CONN-META:listAll]")
            cur.execute(sql, (*params, provider, user_id))
            rows = cur.fetchall()
        return [_row_to_connection_dict(r) for r in rows]
    except Exception:
        logger.exception(
            "Error listing connections for user=%s provider=%s",
            hash_for_log(user_id),
            safe_provider(provider),
        )
        return []
    finally:
        if conn:
            conn.close()


def get_user_connection(
    user_id: str, provider: str, account_id: str
) -> Optional[Dict]:
    """Fetch a single active connection by (user_id, provider, account_id).

    Honors org sharing: org-shared rows in the same org are visible.
    """
    where, params = _gcp_visibility_clause(user_id)

    sql = f"""
        SELECT {_GCP_SELECT_COLUMNS}
        FROM user_connections
        WHERE {where} AND provider = %s AND account_id = %s AND status = 'active'
        LIMIT 1;
    """
    conn = None
    try:
        conn = connect_to_db_as_user()
        with conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[CONN-META:getOne]")
            cur.execute(sql, (*params, provider, account_id))
            row = cur.fetchone()
        return _row_to_connection_dict(row) if row else None
    except Exception:
        logger.exception(
            "Error fetching connection user=%s provider=%s",
            hash_for_log(user_id),
            safe_provider(provider),
        )
        return None
    finally:
        if conn:
            conn.close()


def find_connection_for_project(
    user_id: str, provider: str, project_id: str
) -> Optional[Dict]:
    """Resolve a connection row that owns or has access to ``project_id``.

    Prefers a row whose ``project_id`` (the SA's home project) matches exactly.
    Falls back to any row whose ``accessible_project_ids`` JSONB array contains
    ``project_id``. Used by the GCP auth layer to pick the right SA for a
    project referenced in an alert payload.
    """
    if not project_id:
        return None

    where, params = _gcp_visibility_clause(user_id)

    # accessible_project_ids may be stored in either shape:
    #   ["foo", "bar"]                              ← canonical (new code)
    #   [{"project_id": "foo", "name": "Foo"}, …]  ← legacy connect-route shape
    # Match both via two @> probes joined by OR.
    sql = f"""
        SELECT {_GCP_SELECT_COLUMNS}
        FROM user_connections
        WHERE {where}
          AND provider = %s
          AND status = 'active'
          AND (
                project_id = %s
                OR accessible_project_ids @> %s::jsonb
                OR accessible_project_ids @> %s::jsonb
              )
        ORDER BY
            CASE WHEN project_id = %s THEN 0 ELSE 1 END,
            CASE WHEN user_id = %s THEN 0 ELSE 1 END
        LIMIT 1;
    """
    project_jsonb_str = json.dumps([project_id])
    project_jsonb_obj = json.dumps([{"project_id": project_id}])
    conn = None
    try:
        conn = connect_to_db_as_user()
        with conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[CONN-META:findProj]")
            cur.execute(
                sql,
                (
                    *params,
                    provider,
                    project_id,
                    project_jsonb_str,
                    project_jsonb_obj,
                    project_id,
                    user_id,
                ),
            )
            row = cur.fetchone()
        return _row_to_connection_dict(row) if row else None
    except Exception:
        logger.exception(
            "Error finding connection for project=%s user=%s provider=%s",
            hash_for_log(project_id),
            hash_for_log(user_id),
            safe_provider(provider),
        )
        return None
    finally:
        if conn:
            conn.close()


def deactivate_connection(
    user_id: str, provider: str, account_id: str
) -> Tuple[bool, Optional[str]]:
    """Atomically flip a single connection to ``status='inactive'``.

    Guarded on ``status='active'`` so concurrent deactivates don't race. Returns
    ``(True, secret_ref)`` on success so the caller can delete the Vault secret;
    ``(False, None)`` if nothing was updated.
    """
    sql_select = (
        "SELECT secret_ref FROM user_connections "
        "WHERE user_id = %s AND provider = %s AND account_id = %s "
        "  AND status = 'active' "
        "FOR UPDATE;"
    )
    sql_update = (
        "UPDATE user_connections "
        "SET status = 'inactive', last_verified_at = %s "
        "WHERE user_id = %s AND provider = %s AND account_id = %s "
        "  AND status = 'active';"
    )
    conn = None
    try:
        conn = connect_to_db_as_admin()
        with conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[CONN-META:deactivate]")
            cur.execute(sql_select, (user_id, provider, account_id))
            row = cur.fetchone()
            if not row:
                conn.commit()
                return False, None
            secret_ref = row[0]
            cur.execute(
                sql_update,
                (datetime.now(timezone.utc), user_id, provider, account_id),
            )
        conn.commit()
        logger.info(
            "[CONN-META] Deactivated user=%s provider=%s account=%s",
            hash_for_log(user_id),
            safe_provider(provider),
            hash_for_log(account_id),
        )
        return True, secret_ref
    except Exception:
        logger.exception("[CONN-META] Failed to deactivate connection")
        if conn:
            conn.rollback()
        return False, None
    finally:
        if conn:
            conn.close()


def deactivate_all_connections(
    user_id: str, provider: str
) -> Tuple[bool, List[str]]:
    """Flip every active row for (user_id, provider) to inactive.

    Returns ``(True, [secret_refs])`` so the caller can delete each Vault
    secret. The deactivation is scoped strictly to ``user_id`` (not org) so
    one user disconnecting doesn't wipe an org-mate's credentials.
    """
    sql_select = (
        "SELECT secret_ref FROM user_connections "
        "WHERE user_id = %s AND provider = %s AND status = 'active' "
        "FOR UPDATE;"
    )
    sql_update = (
        "UPDATE user_connections "
        "SET status = 'inactive', last_verified_at = %s "
        "WHERE user_id = %s AND provider = %s AND status = 'active';"
    )
    conn = None
    try:
        conn = connect_to_db_as_admin()
        with conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[CONN-META:deactivateAll]")
            cur.execute(sql_select, (user_id, provider))
            refs = [r[0] for r in cur.fetchall() if r[0]]
            cur.execute(
                sql_update,
                (datetime.now(timezone.utc), user_id, provider),
            )
        conn.commit()
        logger.info(
            "[CONN-META] Deactivated %d connections user=%s provider=%s",
            len(refs),
            hash_for_log(user_id),
            safe_provider(provider),
        )
        return True, refs
    except Exception:
        logger.exception("[CONN-META] Failed to deactivate all connections")
        if conn:
            conn.rollback()
        return False, []
    finally:
        if conn:
            conn.close()


def get_inactive_aws_connections(user_id: str) -> List[Dict]:
    """Return inactive AWS connections for a user."""
    sql = """
        SELECT account_id, role_arn, region, last_verified_at
        FROM user_connections
        WHERE user_id = %s AND provider = 'aws' AND status = 'inactive'
        ORDER BY last_verified_at DESC;
    """
    conn = None
    try:
        conn = connect_to_db_as_user()
        with conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[ConnUtils]")
            cur.execute(sql, (user_id,))
            rows = cur.fetchall()
        return [
            {
                "account_id": r[0],
                "role_arn": r[1],
                "region": r[2],
                "disconnected_at": r[3].isoformat() if r[3] else None,
            }
            for r in rows
        ]
    except Exception as e:
        logger.error("Error listing inactive AWS connections for user %s: %s", user_id, e)
        return []
    finally:
        if conn:
            conn.close()


def get_inactive_aws_connection(user_id: str, account_id: str) -> Optional[Dict]:
    """Get a specific inactive AWS connection by account_id."""
    sql = """
        SELECT role_arn, region
        FROM user_connections
        WHERE user_id = %s AND provider = 'aws' AND account_id = %s AND status = 'inactive'
        LIMIT 1;
    """
    conn = None
    try:
        conn = connect_to_db_as_user()
        with conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[ConnUtils]")
            cur.execute(sql, (user_id, account_id))
            row = cur.fetchone()
        if row:
            return {"role_arn": row[0], "region": row[1]}
        return None
    except Exception as e:
        logger.error("Error getting inactive AWS connection for user %s account %s: %s", user_id, account_id, e)
        return None
    finally:
        if conn:
            conn.close()
