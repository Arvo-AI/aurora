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
    "slack",    # Slack connector tokens
    "confluence", # Confluence connector tokens
    "aurora",   # Aurora-managed SSH keys
}


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
        """
        Store a secret in Vault and return the reference.

        Args:
            secret_name: Name of the secret
            secret_value: The actual secret value to store

        Returns:
            Secret reference string (Vault format)
        """
        # Delegate to Vault backend (which handles logging)
        return self.backend.store_secret(
            secret_name=secret_name,
            secret_value=secret_value,
        )

    def get_secret(self, secret_ref: str) -> str:
        """
        Retrieve a secret from Vault using a reference.

        Args:
            secret_ref: Secret reference (Vault format)

        Returns:
            The actual secret value
        """
        # Check cache first
        cached_secret = get_cached_secret(secret_ref)
        if cached_secret is not None:
            return cached_secret

        # Retrieve from Vault backend (which handles logging)
        secret_value = self.backend.get_secret(secret_ref)

        # Store in cache for future requests
        update_secret_cache(secret_ref, secret_value)

        return secret_value

    def delete_secret(self, secret_ref: str) -> bool:
        """
        Delete a secret from Vault using its reference.

        Args:
            secret_ref: Secret reference (Vault format)

        Returns:
            True if successful, False otherwise
        """
        try:
            self.backend.delete_secret(secret_ref)
            clear_secret_cache(secret_ref)
            return True
        except Exception as e:
            logger.error("Failed to delete secret: %s", e)
            return False

    # ------------------------------------------------------------------
    # Database operations (unchanged)
    # ------------------------------------------------------------------

    def update_user_token_with_secret_ref(self, user_id: str, provider: str, secret_ref: str) -> bool:
        """
        Update a user's token record to use a secret reference instead of storing the token directly.

        Args:
            user_id: User ID
            provider: Provider name (gcp, aws, azure, etc.)
            secret_ref: Secret reference string

        Returns:
            True if successful, False otherwise
        """
        conn = None
        cursor = None
        try:
            conn = connect_to_db_as_admin()
            cursor = conn.cursor()

            cursor.execute(
                "UPDATE user_tokens SET secret_ref = %s, is_active = TRUE WHERE user_id = %s AND provider = %s",
                (secret_ref, user_id, provider)
            )

            if cursor.rowcount > 0:
                conn.commit()
                logger.info("Updated secret_ref for user %s, provider %s", user_id, provider)
                return True
            else:
                logger.warning("No record found for user %s, provider %s", user_id, provider)
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
        """
        Lightweight check if user has credentials stored (without accessing secrets).

        Args:
            user_id: User ID (authenticated userId)
            provider: Provider name

        Returns:
            True if credentials exist, False otherwise
        """
        # Fast exit: if provider not managed in Vault, skip DB query
        provider_base = provider.lower().split('_')[0]
        if provider_base not in SUPPORTED_SECRET_PROVIDERS:
            return False

        conn = None
        cursor = None
        try:
            conn = connect_to_db_as_admin()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM user_tokens WHERE user_id = %s AND provider = %s AND secret_ref IS NOT NULL AND is_active = TRUE LIMIT 1",
                (user_id, provider)
            )
            return cursor.fetchone() is not None
        except Exception as e:
            logger.debug("Error checking credentials for user %s, provider %s: %s", user_id, provider, e)
            return False
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()

    def get_user_token_data(self, user_id: str, provider: str) -> Optional[Dict[str, Any]]:
        """
        Get user token data from Vault.

        Args:
            user_id: User ID (authenticated userId)
            provider: Provider name

        Returns:
            Token data as dictionary, or None if not found
        """
        # Fast-exit for providers not stored in Vault
        provider_base = provider.lower().split('_')[0]
        if provider_base not in SUPPORTED_SECRET_PROVIDERS:
            return None

        conn = None
        cursor = None

        try:
            conn = connect_to_db_as_admin()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT secret_ref, client_id, client_secret FROM user_tokens WHERE user_id = %s AND provider = %s AND secret_ref IS NOT NULL AND is_active = TRUE",
                (user_id, provider)
            )

            result = cursor.fetchone()
            if not result:
                logger.debug("No secret reference found for user %s, provider %s", user_id, provider)
                return None

            secret_ref, role_arn, external_id_secret_ref = result

            # Fetch credentials from Vault
            secret_value = self.get_secret(secret_ref)

            # Parse the secret value
            try:
                token_data = json.loads(secret_value)

                # For AWS, enhance token_data with metadata
                if provider == "aws":
                    if role_arn:
                        token_data["role_arn"] = role_arn
                    # Retrieve external_id from separate secret if available
                    if external_id_secret_ref:
                        try:
                            external_id = self.get_secret(external_id_secret_ref)
                            if external_id:
                                token_data["external_id"] = external_id
                        except Exception as e:
                            logger.warning("Failed to retrieve AWS external_id: %s", e)

                return token_data

            except json.JSONDecodeError:
                # If not JSON, return as plain token
                return {"token": secret_value}

        except Exception as e:
            error_msg = str(e) if e else repr(e)
            error_type = type(e).__name__
            logger.error(
                "Failed to get token data for user %s, provider %s: %s (%s)",
                user_id,
                provider,
                error_msg or "Unknown error",
                error_type,
            )

            # If the secret doesn't exist anymore, clear the secret_ref so future checks are fast
            error_str = error_msg.lower()
            if "not found" in error_str or "no versions" in error_str or "invalidpath" in error_str:
                logger.info(
                    "Secret not found in Vault for user %s, provider %s. Clearing stale secret_ref.",
                    user_id,
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
        """
        Migrate an existing token from token_data column to Vault.

        Args:
            user_id: User ID
            provider: Provider name
            secret_name_prefix: Prefix for the secret name

        Returns:
            True if successful, False otherwise
        """
        conn = None
        cursor = None
        try:
            conn = connect_to_db_as_admin()
            cursor = conn.cursor()

            # Get current token data
            cursor.execute(
                "SELECT token_data FROM user_tokens WHERE user_id = %s AND provider = %s AND secret_ref IS NULL",
                (user_id, provider)
            )

            result = cursor.fetchone()
            if not result:
                logger.info("No token data to migrate for user %s, provider %s", user_id, provider)
                return False

            token_data = result[0]

            # Create secret name
            # Sanitize user_id for secret name (remove special characters)
            safe_user_id = ''.join(c for c in user_id if c.isalnum() or c in '-_')
            secret_name = f"{secret_name_prefix}-{safe_user_id}-{provider}-token"

            # Store in Vault
            token_json = json.dumps(token_data) if isinstance(token_data, dict) else str(token_data)
            secret_ref = self.store_secret(secret_name, token_json)

            # Update database record with secret reference
            cursor.execute(
                "UPDATE user_tokens SET secret_ref = %s WHERE user_id = %s AND provider = %s",
                (secret_ref, user_id, provider)
            )

            conn.commit()
            logger.info("Successfully migrated token to Vault for user %s, provider %s", user_id, provider)
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
        conn = None
        cursor = None
        try:
            conn = connect_to_db_as_admin()
            cursor = conn.cursor()
            # Some deployments have secret_ref column defined as NOT NULL. Use empty string instead of NULL.
            cursor.execute(
                "UPDATE user_tokens SET is_active = FALSE, secret_ref = '' WHERE user_id = %s AND provider = %s",
                (user_id, provider),
            )
            conn.commit()
            logger.info(
                "Cleared stale secret_ref for user %s / provider %s (secret not found)",
                user_id,
                provider,
            )
        except Exception as e:
            logger.warning("Failed to clear stale secret_ref for %s/%s: %s", user_id, provider, e)
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()

    def delete_user_secret(self, user_id: str, provider: str) -> Tuple[bool, int]:
        """
        Delete a user's secret from Vault and clear its reference from the database.

        Returns:
            A tuple containing:
            - bool: True if secret deletion was successful (or not needed), False otherwise.
            - int: The number of rows deleted from the database.
        """
        conn = None
        cursor = None
        delete_success = True
        deleted_rows = 0

        try:
            conn = connect_to_db_as_admin()
            cursor = conn.cursor()

            # Retrieve the secret_ref before deleting from DB
            cursor.execute(
                "SELECT secret_ref FROM user_tokens WHERE user_id = %s AND provider = %s AND secret_ref IS NOT NULL",
                (user_id, provider)
            )
            result = cursor.fetchone()

            if result:
                secret_ref = result[0]
                delete_success = self.delete_secret(secret_ref)
                if not delete_success:
                    logger.warning("Failed to delete secret from Vault for user %s, provider %s", user_id, provider)

            # Always clear the database entry
            cursor.execute(
                "DELETE FROM user_tokens WHERE user_id = %s AND provider = %s",
                (user_id, provider)
            )
            deleted_rows = cursor.rowcount
            conn.commit()

            if deleted_rows > 0:
                logger.info("Deleted credentials for user %s, provider %s", user_id, provider)

            return delete_success, deleted_rows

        except Exception as e:
            logger.error("Failed to delete user secret for %s/%s: %s", user_id, provider, e)
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
    """
    Lightweight check if user has credentials stored (without accessing secrets).
    This function provides a fast way to check connection status.

    Args:
        user_id: User ID (authenticated userId)
        provider: Provider name
    """
    return secret_manager.has_user_credentials(user_id, provider)


def get_user_token_data(user_id: str, provider: str) -> Optional[Dict[str, Any]]:
    """
    Get user token data, automatically handling secret references.
    This function provides backward compatibility with existing code.

    Args:
        user_id: User ID (authenticated userId)
        provider: Provider name
    """
    return secret_manager.get_user_token_data(user_id, provider)


def migrate_user_token_to_secret_ref(user_id: str, provider: str) -> bool:
    """
    Migrate a user's token from database storage to Vault.
    """
    return secret_manager.migrate_token_to_secret_ref(user_id, provider)


def delete_user_secret(user_id: str, provider: str) -> Tuple[bool, int]:
    """
    Delete a user's secret from Vault and clear its reference from the database.
    """
    return secret_manager.delete_user_secret(user_id, provider)
