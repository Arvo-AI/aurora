"""
Utility functions for handling secret references in the database.

This module provides functions to store and retrieve secrets using HashiCorp
Vault instead of storing actual token data in the database.
"""

import logging
import json
from typing import TYPE_CHECKING, Optional, Dict, Any, Set, Tuple

from utils.db.db_utils import connect_to_db_as_admin
from utils.secrets.secret_cache import (
    get_cached_secret,
    update_secret_cache,
    clear_secret_cache,
)

if TYPE_CHECKING:
    from utils.secrets.base import SecretsBackend

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------

# Providers whose credentials are managed through Vault.
# All other providers fall back to legacy storage (e.g. raw `token_data` column)
# and therefore should **not** trigger Vault look-ups or warnings.
#
# NOTE: keep this list in lowercase for case-insensitive comparison.

SUPPORTED_SECRET_PROVIDERS: Set[str] = {
    "gcp",      # Google Cloud
    "aws",      # Amazon Web Services
    "azure",    # Microsoft Azure
    "github",   # GitHub tokens
    "github_repo_selection",  # GitHub selected repository and branch
    "grafana",  # Grafana connector tokens
    "datadog",  # Datadog connector tokens
    "netdata",  # Netdata connector tokens
    "pagerduty", # PagerDuty connector tokens
    "splunk",    # Splunk connector tokens
    "ovh",      # OVH Cloud
    "scaleway", # Scaleway Cloud
    "tailscale", # Tailscale VPN
    "cloudflare", # Cloudflare (DNS, Workers, WAF, analytics)
    "slack",    # Slack connector tokens
    "confluence", # Confluence connector tokens
    "jira",       # Jira connector tokens
    "sharepoint", # SharePoint connector tokens
    "coroot",   # Coroot connector tokens
    "bitbucket", # Bitbucket connector tokens
    "bitbucket_workspace_selection",  # Bitbucket selected workspace and repo
    "dynatrace", # Dynatrace connector tokens
    "bigpanda", # BigPanda connector tokens
    "thousandeyes", # ThousandEyes connector tokens
    "aurora",   # Aurora-managed SSH keys
    "jenkins",  # Jenkins CI/CD connector tokens
    "cloudbees", # CloudBees CI connector tokens
    "spinnaker", # Spinnaker CD connector tokens
    "newrelic",  # New Relic connector tokens
}


def _resolve_org(user_id: str) -> Optional[str]:
    """Best-effort org_id resolution for use in admin-connection queries."""
    try:
        from utils.auth.stateless_auth import resolve_org_id
        return resolve_org_id(user_id)
    except Exception:
        return None


def _org_clause(org_id: Optional[str]) -> Tuple[str, Tuple]:
    """Build a reusable SQL fragment for org-scoped token queries.

    Returns (sql_fragment, params) to append to a WHERE clause.
    When org_id is available the fragment restricts to matching rows;
    otherwise it returns an always-true fragment so the query still works.
    """
    if org_id:
        return "AND (org_id = %s OR org_id IS NULL)", (org_id,)
    return "", ()


class SecretRefManager:
    """Manager for handling secret references in the database.

    Uses HashiCorp Vault for actual secret storage. The backend is lazily
    initialized on first use.
    """

    def __init__(self) -> None:
        self._backend: Optional["SecretsBackend"] = None

    # ------------------------------------------------------------------
    # Backend access
    # ------------------------------------------------------------------

    @property
    def backend(self) -> "SecretsBackend":
        """Lazily initialize and return the Vault secrets backend."""
        if self._backend is None:
            from utils.secrets import get_secrets_backend
            self._backend = get_secrets_backend()
        return self._backend

    def is_available(self) -> bool:
        """Check if the secrets backend (Vault) is available."""
        return self.backend.is_available()

    # ------------------------------------------------------------------
    # Secret operations (delegated to Vault backend)
    # ------------------------------------------------------------------

    def store_secret(self, secret_name: str, secret_value: str) -> str:
        """Store a secret in Vault and return the reference."""
        return self.backend.store_secret(
            secret_name=secret_name,
            secret_value=secret_value,
        )

    def get_secret(self, secret_ref: str) -> str:
        """Retrieve a secret from Vault using a reference."""
        cached_secret = get_cached_secret(secret_ref)
        if cached_secret is not None:
            return cached_secret

        secret_value = self.backend.get_secret(secret_ref)
        update_secret_cache(secret_ref, secret_value)
        return secret_value

    def delete_secret(self, secret_ref: str) -> bool:
        """Delete a secret from Vault using its reference."""
        try:
            self.backend.delete_secret(secret_ref)
            clear_secret_cache(secret_ref)
            return True
        except Exception as e:
            logger.error("Failed to delete secret: %s", e)
            return False

    # ------------------------------------------------------------------
    # Database operations
    # ------------------------------------------------------------------

    def update_user_token_with_secret_ref(self, user_id: str, provider: str, secret_ref: str) -> bool:
        """Update a user's token record to point at a Vault secret reference."""
        org_id = _resolve_org(user_id)
        clause, params = _org_clause(org_id)
        conn = None
        cursor = None
        try:
            conn = connect_to_db_as_admin()
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE user_tokens SET secret_ref = %s, is_active = TRUE "
                f"WHERE user_id = %s AND provider = %s {clause}",
                (secret_ref, user_id, provider) + params,
            )
            if cursor.rowcount > 0:
                conn.commit()
                logger.info("Updated secret_ref for provider %s", provider)
                return True
            logger.warning("No record found for provider %s", provider)
            return False
        except Exception as e:
            logger.error("Failed to update secret_ref: %s", e)
            if conn:
                conn.rollback()
            return False
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()

    def has_user_credentials(self, user_id: str, provider: str) -> bool:
        """Lightweight check if user (or their org) has credentials stored."""
        provider_base = provider.lower().split('_')[0]
        if provider_base not in SUPPORTED_SECRET_PROVIDERS:
            return False

        org_id = _resolve_org(user_id)
        conn = None
        cursor = None
        try:
            conn = connect_to_db_as_admin()
            cursor = conn.cursor()
            cursor.execute(
                """SELECT 1 FROM user_tokens
                   WHERE (user_id = %s OR org_id = %s)
                     AND provider = %s
                     AND secret_ref IS NOT NULL
                     AND is_active = TRUE
                   LIMIT 1""",
                (user_id, org_id, provider)
            )
            return cursor.fetchone() is not None
        except Exception as e:
            logger.debug("Error checking credentials for provider %s: %s", provider, e)
            return False
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()

    def get_user_token_data(self, user_id: str, provider: str) -> Optional[Dict[str, Any]]:
        """Get user token data from Vault."""
        provider_base = provider.lower().split('_')[0]
        if provider_base not in SUPPORTED_SECRET_PROVIDERS:
            return None

        org_id = _resolve_org(user_id)
        conn = None
        cursor = None

        try:
            conn = connect_to_db_as_admin()
            cursor = conn.cursor()
            cursor.execute(
                """SELECT secret_ref, client_id, client_secret
                   FROM user_tokens
                   WHERE (user_id = %s OR org_id = %s)
                     AND provider = %s
                     AND secret_ref IS NOT NULL
                     AND is_active = TRUE
                   ORDER BY CASE WHEN user_id = %s THEN 0 ELSE 1 END
                   LIMIT 1""",
                (user_id, org_id, provider, user_id)
            )

            result = cursor.fetchone()
            if not result:
                logger.debug("No secret reference found for provider %s", provider)
                return None

            secret_ref, role_arn, external_id_secret_ref = result
            secret_value = self.get_secret(secret_ref)

            try:
                token_data = json.loads(secret_value)

                if provider == "aws":
                    if role_arn:
                        token_data["role_arn"] = role_arn
                    if external_id_secret_ref:
                        try:
                            external_id = self.get_secret(external_id_secret_ref)
                            if external_id:
                                token_data["external_id"] = external_id
                        except Exception as e:
                            logger.warning("Failed to retrieve AWS external_id: %s", e)

                return token_data

            except json.JSONDecodeError:
                return {"token": secret_value}

        except Exception as e:
            error_msg = str(e) if e else repr(e)
            error_type = type(e).__name__
            logger.error(
                "Failed to get token data for provider %s: %s (%s)",
                provider, error_msg or "Unknown error", error_type,
            )

            error_str = error_msg.lower()
            if "not found" in error_str or "no versions" in error_str or "invalidpath" in error_str:
                logger.info(
                    "Secret not found in Vault for provider %s. Clearing stale secret_ref.",
                    provider,
                )
                self._clear_secret_ref(user_id, provider)

            return None
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()

    def migrate_token_to_secret_ref(self, user_id: str, provider: str, secret_name_prefix: str = "aurora-dev") -> bool:
        """Migrate an existing token from token_data column to Vault."""
        org_id = _resolve_org(user_id)
        clause, params = _org_clause(org_id)
        conn = None
        cursor = None
        try:
            conn = connect_to_db_as_admin()
            cursor = conn.cursor()

            cursor.execute(
                f"SELECT token_data FROM user_tokens "
                f"WHERE user_id = %s AND provider = %s {clause} AND secret_ref IS NULL",
                (user_id, provider) + params,
            )

            result = cursor.fetchone()
            if not result:
                logger.info("No token data to migrate for provider %s", provider)
                return False

            token_data = result[0]
            safe_user_id = ''.join(c for c in user_id if c.isalnum() or c in '-_')
            secret_name = f"{secret_name_prefix}-{safe_user_id}-{provider}-token"

            token_json = json.dumps(token_data) if isinstance(token_data, dict) else str(token_data)
            secret_ref = self.store_secret(secret_name, token_json)

            cursor.execute(
                f"UPDATE user_tokens SET secret_ref = %s "
                f"WHERE user_id = %s AND provider = %s {clause}",
                (secret_ref, user_id, provider) + params,
            )

            conn.commit()
            logger.info("Successfully migrated token to Vault for provider %s", provider)
            return True

        except Exception as e:
            logger.error("Failed to migrate token to Vault: %s", e)
            if conn:
                conn.rollback()
            return False
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()

    # ------------------------------------------------------------------
    # Database helpers
    # ------------------------------------------------------------------

    def _clear_secret_ref(self, user_id: str, provider: str) -> None:
        """Set secret_ref to NULL for the given user/provider (stale reference cleanup)."""
        org_id = _resolve_org(user_id)
        clause, params = _org_clause(org_id)
        conn = None
        cursor = None
        try:
            conn = connect_to_db_as_admin()
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE user_tokens SET is_active = FALSE, secret_ref = '' "
                f"WHERE user_id = %s AND provider = %s {clause}",
                (user_id, provider) + params,
            )
            conn.commit()
            logger.info(
                "Cleared stale secret_ref for provider %s (secret not found)",
                provider,
            )
        except Exception as e:
            logger.warning("Failed to clear stale secret_ref for provider %s: %s", provider, e)
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()

    def delete_user_secret(self, user_id: str, provider: str) -> Tuple[bool, int]:
        """Delete a user's secret from Vault and clear its reference from the database."""
        org_id = _resolve_org(user_id)
        clause, params = _org_clause(org_id)
        conn = None
        cursor = None
        delete_success = True
        deleted_rows = 0

        try:
            conn = connect_to_db_as_admin()
            cursor = conn.cursor()

            cursor.execute(
                f"SELECT secret_ref FROM user_tokens "
                f"WHERE user_id = %s AND provider = %s {clause} AND secret_ref IS NOT NULL",
                (user_id, provider) + params,
            )
            result = cursor.fetchone()

            if result:
                secret_ref = result[0]
                delete_success = self.delete_secret(secret_ref)
                if not delete_success:
                    logger.warning("Failed to delete secret from Vault for provider %s", provider)

            cursor.execute(
                f"DELETE FROM user_tokens WHERE user_id = %s AND provider = %s {clause}",
                (user_id, provider) + params,
            )
            deleted_rows = cursor.rowcount
            conn.commit()

            if deleted_rows > 0:
                logger.info("Deleted credentials for provider %s", provider)

            return delete_success, deleted_rows

        except Exception as e:
            logger.error("Failed to delete user secret for provider %s: %s", provider, e)
            if conn:
                conn.rollback()
            return False, 0
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()


# Convenience functions for backward compatibility
secret_manager = SecretRefManager()


def has_user_credentials(user_id: str, provider: str) -> bool:
    """Lightweight check if user has credentials stored (without accessing secrets)."""
    return secret_manager.has_user_credentials(user_id, provider)


def get_user_token_data(user_id: str, provider: str) -> Optional[Dict[str, Any]]:
    """Get user token data, automatically handling secret references."""
    return secret_manager.get_user_token_data(user_id, provider)


def migrate_user_token_to_secret_ref(user_id: str, provider: str) -> bool:
    """Migrate a user's token from database storage to Vault."""
    return secret_manager.migrate_token_to_secret_ref(user_id, provider)


def delete_user_secret(user_id: str, provider: str) -> Tuple[bool, int]:
    """Delete a user's secret from Vault and clear its reference from the database."""
    return secret_manager.delete_user_secret(user_id, provider)
