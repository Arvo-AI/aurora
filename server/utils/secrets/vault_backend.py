"""
HashiCorp Vault secrets backend implementation.

Uses the HVAC Python client to interact with Vault's KV v2 secrets engine.
This is the OSS-first default for Aurora deployments.
"""

import os
import logging
import threading
import time
from typing import Optional

from .base import SecretsBackend

logger = logging.getLogger(__name__)

# Vault reference prefix for identifying Vault-stored secrets
VAULT_REF_PREFIX = "vault:kv/data/"


class VaultSecretsBackend(SecretsBackend):
    """HashiCorp Vault secrets backend using KV v2 secrets engine.

    Configuration via environment variables:
    - VAULT_ADDR: Vault server address (default: http://vault:8200)
    - VAULT_TOKEN: Authentication token (required for token auth)
    - VAULT_KV_MOUNT: KV secrets engine mount path (default: aurora)
    - VAULT_KV_BASE_PATH: Base path for secrets (default: users)

    Secret reference format:
        vault:kv/data/{mount}/{base_path}/{secret_name}
    """

    # Minimum gap between initialization attempts once one has failed.
    RETRY_INTERVAL_SECONDS = 30
    # Cap on the auth probe, which runs while _init_lock is held.
    CONNECT_TIMEOUT_SECONDS = 10

    def __init__(self):
        self._client = None
        self._initialized = False
        self._available = False
        # Monotonic timestamp of the last failed init, so a transient failure is
        # retried instead of disabling Vault for the life of the process.
        self._last_failure_at = None
        self._init_lock = threading.Lock()
        self.mount_point = os.getenv("VAULT_KV_MOUNT", "aurora")
        self.vault_addr = os.getenv("VAULT_ADDR", "http://vault:8200")
        self.base_path = os.getenv("VAULT_KV_BASE_PATH", "users")

    def _initialize_client(self):
        """Lazily initialize the Vault client, retrying after a failure.

        Success latches: the client is reused for the rest of the process. Failure
        does NOT latch. Vault can be unreachable, sealed, or holding an expired
        token when a pod first touches it, and latching `_available = False` there
        left every secret lookup failing until the pod was restarted — long after
        Vault itself had recovered. Failures are retried, at most once per
        RETRY_INTERVAL_SECONDS so a genuine outage doesn't turn every lookup into a
        blocking connection attempt.
        """
        if self._initialized:
            return

        # Checked before taking the lock: the lock is held across a network call,
        # so during an outage this keeps callers from queueing behind it instead of
        # failing fast on the backoff they are already inside.
        if self._in_retry_backoff():
            return

        with self._init_lock:
            # Another thread initialized or failed while this one waited.
            if self._initialized or self._in_retry_backoff():
                return

            if self._try_initialize():
                self._initialized = True
                self._available = True
                self._last_failure_at = None
            else:
                self._available = False
                self._last_failure_at = time.monotonic()

    def _in_retry_backoff(self) -> bool:
        """True while a recent failed attempt should suppress another one."""
        last_failure = self._last_failure_at
        if last_failure is None:
            return False
        return (time.monotonic() - last_failure) < self.RETRY_INTERVAL_SECONDS

    def _try_initialize(self) -> bool:
        """One initialization attempt. True on success; never raises."""
        try:
            import hvac
        except ImportError:
            logger.warning(
                "hvac package not installed. Install with: pip install hvac"
            )
            return False

        vault_token = os.getenv("VAULT_TOKEN")

        if not vault_token:
            logger.warning(
                "VAULT_TOKEN not set. Vault secrets backend will not be available."
            )
            return False

        try:
            # Bounded: this call runs under _init_lock, so an unbounded wait on an
            # unreachable Vault would stall every other caller behind it.
            client = hvac.Client(
                url=self.vault_addr,
                token=vault_token,
                timeout=self.CONNECT_TIMEOUT_SECONDS,
            )

            # Verify connection and authentication
            if not client.is_authenticated():
                logger.error(
                    "Vault authentication failed (unreachable, sealed, or expired "
                    "VAULT_TOKEN). Will retry in %ss.",
                    self.RETRY_INTERVAL_SECONDS,
                )
                return False

            self._client = client

            # Auto-enable KV v2 engine if not already enabled
            self._ensure_kv_engine()

            logger.info(
                "VaultSecretsBackend initialized (addr: %s, mount: %s, base_path: %s)",
                self.vault_addr,
                self.mount_point,
                self.base_path,
            )
            return True

        except Exception as e:
            logger.error(
                "Failed to initialize Vault client: %s. Will retry in %ss.",
                e,
                self.RETRY_INTERVAL_SECONDS,
            )
            return False

    def _ensure_kv_engine(self):
        """Enable KV v2 secrets engine if not already enabled."""
        try:
            # List existing mounts
            mounts = self._client.sys.list_mounted_secrets_engines()
            mount_path = f"{self.mount_point}/"

            if mount_path not in mounts:
                logger.info("Enabling KV v2 secrets engine at '%s'", self.mount_point)
                self._client.sys.enable_secrets_engine(
                    backend_type="kv",
                    path=self.mount_point,
                    options={"version": "2"},
                )
        except Exception as e:
            # Log but don't fail - mount might already exist or we lack permissions
            logger.debug("Could not auto-enable KV engine: %s", e)

    def is_available(self) -> bool:
        """Check if Vault backend is configured and available."""
        if not self._initialized:
            self._initialize_client()
        return self._available

    def can_handle_ref(self, secret_ref: str) -> bool:
        """Check if this is a Vault secret reference."""
        return secret_ref.startswith(VAULT_REF_PREFIX)

    def build_system_ref(self, logical_name: str) -> str:
        """Build a Vault reference for a system-scoped secret.

        System secrets live under a ``system/`` base path (distinct from the
        per-user ``base_path``) at ``vault:kv/data/{mount}/system/{logical_name}``.
        For the default mount ``aurora`` and ``github-app/private-key`` this
        yields ``vault:kv/data/aurora/system/github-app/private-key`` — the
        same path operators already provision with ``vault kv put``.
        """
        return f"{VAULT_REF_PREFIX}{self.mount_point}/system/{logical_name}"

    def store_secret(self, secret_name: str, secret_value: str, **kwargs) -> str:
        """Store a secret in Vault KV v2.

        Args:
            secret_name: Name/identifier for the secret
            secret_value: The secret data to store
            **kwargs: Ignored (for interface compatibility)

        Returns:
            Reference string in format: vault:kv/data/{mount}/users/{name}
        """
        start_time = time.perf_counter()

        if not self._initialized:
            self._initialize_client()

        if not self._available or not self._client:
            raise RuntimeError(
                "Vault secrets backend is not available. "
                "Check VAULT_ADDR and VAULT_TOKEN configuration."
            )

        try:
            path = f"{self.base_path}/{secret_name}"

            # Store the secret in KV v2
            self._client.secrets.kv.v2.create_or_update_secret(
                mount_point=self.mount_point,
                path=path,
                secret={"value": secret_value},
            )

            elapsed_ms = (time.perf_counter() - start_time) * 1000
            secret_ref = f"{VAULT_REF_PREFIX}{self.mount_point}/{path}"

            logger.info("Stored secret '%s' in Vault (%.1fms)", secret_name, elapsed_ms)

            return secret_ref

        except Exception as e:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            logger.error("Failed to store secret '%s' (%.1fms): %s", secret_name, elapsed_ms, e)
            raise

    def get_secret(self, secret_ref: str) -> str:
        """Retrieve a secret from Vault KV v2.

        Args:
            secret_ref: Reference in format vault:kv/data/{mount}/{path}

        Returns:
            The secret value as a string
        """
        start_time = time.perf_counter()

        if not self._initialized:
            self._initialize_client()

        if not self._available or not self._client:
            raise RuntimeError(
                "Vault secrets backend is not available. "
                "Check VAULT_ADDR and VAULT_TOKEN configuration."
            )

        try:
            # Parse the secret reference to extract the path
            # Format: vault:kv/data/{mount}/{path}
            if not secret_ref.startswith(VAULT_REF_PREFIX):
                raise ValueError(f"Invalid Vault secret reference format: {secret_ref}")

            # Extract path after the prefix
            path_with_mount = secret_ref[len(VAULT_REF_PREFIX):]

            # Remove mount point prefix if present
            expected_prefix = f"{self.mount_point}/"
            if path_with_mount.startswith(expected_prefix):
                path = path_with_mount[len(expected_prefix):]
            else:
                path = path_with_mount

            response = self._client.secrets.kv.v2.read_secret_version(
                mount_point=self.mount_point,
                path=path,
                raise_on_deleted_version=True,
            )

            # KV v2 response structure: response['data']['data']['key']
            secret_data = response["data"]["data"]
            secret_value = secret_data.get("value", "")

            elapsed_ms = (time.perf_counter() - start_time) * 1000
            logger.debug("Retrieved secret from Vault (%.1fms)", elapsed_ms)

            return secret_value

        except Exception as e:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            error_msg = str(e) if e else repr(e)
            error_type = type(e).__name__
            logger.error(
                "Failed to retrieve secret (%.1fms): %s (%s), path: %s",
                elapsed_ms,
                error_msg or "Unknown error",
                error_type,
                path if 'path' in locals() else secret_ref,
            )
            raise

    def delete_secret(self, secret_ref: str) -> None:
        """Delete a secret from Vault KV v2.

        This permanently deletes all versions and metadata for the secret.

        Args:
            secret_ref: Reference in format vault:kv/data/{mount}/{path}

        Raises:
            RuntimeError: If Vault backend is not available
            ValueError: If secret reference format is invalid
        """
        start_time = time.perf_counter()

        if not self._initialized:
            self._initialize_client()

        if not self._available or not self._client:
            raise RuntimeError(
                "Vault secrets backend is not available. "
                "Check VAULT_ADDR and VAULT_TOKEN configuration."
            )

        try:
            # Parse the secret reference
            if not secret_ref.startswith(VAULT_REF_PREFIX):
                raise ValueError(f"Invalid Vault secret reference format: {secret_ref}")

            path_with_mount = secret_ref[len(VAULT_REF_PREFIX):]
            expected_prefix = f"{self.mount_point}/"
            if path_with_mount.startswith(expected_prefix):
                path = path_with_mount[len(expected_prefix):]
            else:
                path = path_with_mount

            # Delete all versions and metadata
            self._client.secrets.kv.v2.delete_metadata_and_all_versions(
                mount_point=self.mount_point,
                path=path,
            )

            elapsed_ms = (time.perf_counter() - start_time) * 1000
            logger.info("Deleted secret '%s' from Vault (%.1fms)", path, elapsed_ms)

        except Exception as e:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            logger.error("Failed to delete secret (%.1fms): %s", elapsed_ms, e)
            raise
