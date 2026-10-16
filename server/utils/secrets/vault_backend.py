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
    # Cap only on the throwaway auth probe, which runs while _init_lock is held.
    CONNECT_TIMEOUT_SECONDS = 10

    def __init__(self):
        """Nothing connects here — the client is built lazily on first lookup."""
        self._client = None
        self._initialized = False
        self._available = False
        # Monotonic timestamp of the last failed init, so a transient failure is
        # retried instead of disabling Vault for the life of the process.
        self._last_failure_at = None
        # Missing hvac or VAULT_TOKEN will not self-heal without a redeploy.
        self._config_blocked = False
        self._logged_init_traceback = False
        self._init_lock = threading.Lock()
        self.mount_point = os.getenv("VAULT_KV_MOUNT", "aurora")
        self.vault_addr = os.getenv("VAULT_ADDR", "http://vault:8200")
        self.base_path = os.getenv("VAULT_KV_BASE_PATH", "users")

    def _initialize_client(self):
        """Lazily initialize the Vault client, retrying after a failure.

        Failure must not latch: a pod that first touched Vault while it was
        unreachable, sealed, or holding an expired token kept failing every lookup
        until restarted, long after Vault recovered. Retried at most once per
        RETRY_INTERVAL_SECONDS so an outage doesn't reconnect on every lookup.
        """
        if self._initialized:
            return

        if self._config_blocked:
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
                self._logged_init_traceback = False
            elif not self._config_blocked:
                self._available = False
                self._last_failure_at = time.monotonic()

    def _in_retry_backoff(self) -> bool:
        """True while a recent failed attempt should suppress another one."""
        last_failure = self._last_failure_at
        if last_failure is None:
            return False
        return (time.monotonic() - last_failure) < self.RETRY_INTERVAL_SECONDS

    @staticmethod
    def _is_missing_secret(exc: Exception) -> bool:
        """True when the failure is about one absent secret, not Vault's health.

        Mirrors the classification in ``secret_ref_utils._clear_secret_ref`` so the
        two agree on what "not found" means.
        """
        if type(exc).__name__ == "InvalidPath":
            return True
        text = str(exc).lower()
        return "not found" in text or "no versions" in text or "invalidpath" in text

    @staticmethod
    def _is_vault_outage(exc: Exception) -> bool:
        """True when Vault itself is unreachable or unhealthy, not one path or request."""
        name = type(exc).__name__
        # Per-path ACL or bad request shape — not a cluster health signal.
        if name in ("Forbidden", "InvalidRequest"):
            return False

        if name in (
            "Unauthorized",
            "VaultDown",
            "InternalServerError",
            "BadGateway",
            "VaultNotInitialized",
        ):
            return True

        if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
            return True

        module = type(exc).__module__ or ""
        if module.startswith("requests.exceptions"):
            return True

        text = str(exc).lower()
        return "sealed" in text or "vault is down" in text

    def _invalidate_after_operation_failure(self, exc: Exception, client=None) -> None:
        """Drop the cached availability latch when an operation suggests an outage.

        Availability is otherwise only probed at init, so a Vault that seals *after*
        that keeps reporting available and the outage gets blamed on the provider's
        credentials. A healthy Vault re-latches on the next probe, so this cannot
        invent an outage.
        """
        # A malformed reference is a caller bug and a missing secret is a per-secret
        # condition; neither says anything about Vault's health.
        if (
            not self._initialized
            or isinstance(exc, ValueError)
            or self._is_missing_secret(exc)
            or not self._is_vault_outage(exc)
        ):
            return

        with self._init_lock:
            # A slow failure can land after another thread already re-probed and
            # published a healthy client; only the client that failed may clear it.
            if client is not None and self._client is not client:
                return

            logger.warning(
                "Vault operation failed (%s); re-checking availability on next use.",
                type(exc).__name__,
            )
            self._initialized = False
            self._available = False

    def _try_initialize(self) -> bool:
        """One initialization attempt. True on success; never raises."""
        try:
            import hvac
        except ImportError:
            logger.warning(
                "hvac package not installed. Install with: pip install hvac"
            )
            self._config_blocked = True
            self._initialized = True
            return False

        vault_token = os.getenv("VAULT_TOKEN")

        if not vault_token:
            logger.warning(
                "VAULT_TOKEN not set. Vault secrets backend will not be available."
            )
            self._config_blocked = True
            self._initialized = True
            return False

        try:
            # Short timeout only on the probe: it runs under _init_lock, so an
            # unbounded wait would stall every other caller behind it.
            probe = hvac.Client(
                url=self.vault_addr,
                token=vault_token,
                timeout=self.CONNECT_TIMEOUT_SECONDS,
            )

            if not probe.is_authenticated():
                logger.error(
                    "Vault authentication failed (unreachable, sealed, or expired "
                    "VAULT_TOKEN). Will retry in %ss.",
                    self.RETRY_INTERVAL_SECONDS,
                )
                return False

            # Operations keep hvac's default (30s) request timeout.
            self._client = hvac.Client(url=self.vault_addr, token=vault_token)

            # Auto-enable KV v2 engine if not already enabled
            self._ensure_kv_engine()

            logger.info(
                "VaultSecretsBackend initialized (addr: %s, mount: %s, base_path: %s)",
                self.vault_addr,
                self.mount_point,
                self.base_path,
            )
            return True

        except Exception:
            # Traceback on the first failure only — during an outage every service
            # would otherwise print a full stack every 30s.
            if not self._logged_init_traceback:
                logger.exception(
                    "Failed to initialize Vault client. Will retry in %ss.",
                    self.RETRY_INTERVAL_SECONDS,
                )
                self._logged_init_traceback = True
            else:
                logger.warning(
                    "Failed to initialize Vault client. Will retry in %ss.",
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

        # Pinned for the whole operation so a concurrent re-probe can't swap the
        # client mid-call, and so the failure handler knows which one failed.
        client = self._client

        try:
            path = f"{self.base_path}/{secret_name}"

            # Store the secret in KV v2
            client.secrets.kv.v2.create_or_update_secret(
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
            self._invalidate_after_operation_failure(e, client)
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

        client = self._client

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

            response = client.secrets.kv.v2.read_secret_version(
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
            self._invalidate_after_operation_failure(e, client)
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

        client = self._client

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
            client.secrets.kv.v2.delete_metadata_and_all_versions(
                mount_point=self.mount_point,
                path=path,
            )

            elapsed_ms = (time.perf_counter() - start_time) * 1000
            logger.info("Deleted secret '%s' from Vault (%.1fms)", path, elapsed_ms)

        except Exception as e:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            logger.error("Failed to delete secret (%.1fms): %s", elapsed_ms, e)
            self._invalidate_after_operation_failure(e, client)
            raise
