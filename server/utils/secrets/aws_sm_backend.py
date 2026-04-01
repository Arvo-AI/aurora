"""
AWS Secrets Manager secrets backend implementation.

Uses boto3 to interact with AWS Secrets Manager directly.
This is an alternative to HashiCorp Vault for deployments where
AWS Secrets Manager is the approved secrets store (e.g., EKS with IRSA).
"""

import os
import logging
import threading
import time

from .base import SecretsBackend

logger = logging.getLogger(__name__)

AWSSM_REF_PREFIX = "awssm:"


class AWSSecretsManagerBackend(SecretsBackend):
    """AWS Secrets Manager secrets backend.

    Configuration via environment variables:
    - AWS_SM_REGION: AWS region for Secrets Manager (required)
    - AWS_SM_PREFIX: Path prefix for secret names (default: aurora/users)
    - AWS credentials: standard boto3 chain (env vars, IRSA, instance profile)

    Secret reference format:
        awssm:{region}:{prefix}/{secret_name}
    """

    def __init__(self):
        self._client = None
        self._initialized = False
        self._available = False
        self._init_lock = threading.Lock()
        self.region = os.getenv("AWS_SM_REGION", "")
        self.prefix = os.getenv("AWS_SM_PREFIX", "aurora/users")

    def _initialize_client(self):
        """Lazily initialize the boto3 Secrets Manager client.

        Only attempts initialization once to avoid repeated failures
        on startup when the backend may not be needed.
        Thread-safe via _init_lock.
        """
        with self._init_lock:
            if self._initialized:
                return

            if not self.region:
                logger.warning(
                    "AWS_SM_REGION not set. AWS Secrets Manager backend will not be available."
                )
                self._available = False
                self._initialized = True
                return

            try:
                import boto3
            except ImportError:
                logger.warning(
                    "boto3 package not installed. Install with: pip install boto3"
                )
                self._available = False
                self._initialized = True
                return

            try:
                self._client = boto3.client(
                    "secretsmanager",
                    region_name=self.region,
                )

                self._available = True
                logger.info(
                    "AWSSecretsManagerBackend initialized (region: %s, prefix: %s)",
                    self.region,
                    self.prefix,
                )

            except Exception as e:
                logger.error("Failed to initialize AWS Secrets Manager client: %s", e)
                self._available = False

            self._initialized = True

    def is_available(self) -> bool:
        """Check if AWS Secrets Manager backend is configured and available."""
        if not self._initialized:
            self._initialize_client()
        return self._available

    def can_handle_ref(self, secret_ref: str) -> bool:
        """Check if this is an AWS SM secret reference."""
        return secret_ref.startswith(AWSSM_REF_PREFIX)

    def _parse_ref(self, secret_ref: str) -> str:
        """Extract the secret name from an awssm: reference string."""
        if not secret_ref.startswith(AWSSM_REF_PREFIX):
            raise ValueError("Invalid AWS SM secret reference format: bad prefix")

        ref_body = secret_ref[len(AWSSM_REF_PREFIX):]
        parts = ref_body.split(":", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError("Invalid AWS SM secret reference format: missing region or secret name")

        region = parts[0]
        if region != self.region:
            raise ValueError(
                f"Secret region '{region}' does not match configured AWS_SM_REGION '{self.region}'"
            )
        return parts[1]

    def _ensure_client(self) -> None:
        """Ensure the backend is initialized and available."""
        if not self._initialized:
            self._initialize_client()

        if not self._available or not self._client:
            raise RuntimeError(
                "AWS Secrets Manager backend is not available. "
                "Check AWS_SM_REGION and AWS credentials configuration."
            )

    def store_secret(self, secret_name: str, secret_value: str, **kwargs) -> str:
        """Store a secret in AWS Secrets Manager.

        Updates the secret if it exists, otherwise creates it.

        Args:
            secret_name: Name/identifier for the secret
            secret_value: The secret data to store
            **kwargs: Ignored (for interface compatibility)

        Returns:
            Reference string in format: awssm:{region}:{prefix}/{name}
        """
        start_time = time.perf_counter()
        self._ensure_client()

        full_name = f"{self.prefix}/{secret_name}"

        try:
            # put_secret_value is the common case (update existing secret)
            try:
                self._client.put_secret_value(
                    SecretId=full_name,
                    SecretString=secret_value,
                )
            except self._client.exceptions.ResourceNotFoundException:
                self._client.create_secret(
                    Name=full_name,
                    SecretString=secret_value,
                )
        except Exception as e:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            logger.error(
                "Failed to store secret in AWS Secrets Manager (%.1fms): %s",
                elapsed_ms, e,
            )
            raise

        elapsed_ms = (time.perf_counter() - start_time) * 1000
        secret_ref = f"{AWSSM_REF_PREFIX}{self.region}:{full_name}"

        logger.info(
            "Stored secret in AWS Secrets Manager (%.1fms)",
            elapsed_ms,
        )

        return secret_ref

    def get_secret(self, secret_ref: str) -> str:
        """Retrieve a secret from AWS Secrets Manager.

        Args:
            secret_ref: Reference in format awssm:{region}:{prefix}/{name}

        Returns:
            The secret value as a string
        """
        start_time = time.perf_counter()
        self._ensure_client()

        try:
            secret_name = self._parse_ref(secret_ref)

            response = self._client.get_secret_value(SecretId=secret_name)
            secret_value = response["SecretString"]

            elapsed_ms = (time.perf_counter() - start_time) * 1000
            logger.debug(
                "Retrieved secret from AWS Secrets Manager (%.1fms)", elapsed_ms
            )

            return secret_value

        except Exception as e:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            logger.error(
                "Failed to retrieve secret (%.1fms): %s (%s)",
                elapsed_ms, e, type(e).__name__,
            )
            raise

    def delete_secret(self, secret_ref: str) -> None:
        """Delete a secret from AWS Secrets Manager.

        Uses ForceDeleteWithoutRecovery to immediately remove the secret
        (no 7-30 day recovery window).

        Args:
            secret_ref: Reference in format awssm:{region}:{prefix}/{name}

        Raises:
            RuntimeError: If the backend is not available
            ValueError: If secret reference format is invalid
        """
        start_time = time.perf_counter()
        self._ensure_client()

        try:
            secret_name = self._parse_ref(secret_ref)

            self._client.delete_secret(
                SecretId=secret_name,
                ForceDeleteWithoutRecovery=True,
            )

            elapsed_ms = (time.perf_counter() - start_time) * 1000
            logger.info(
                "Deleted secret from AWS Secrets Manager (%.1fms)",
                elapsed_ms,
            )

        except Exception as e:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            logger.error(
                "Failed to delete secret (%.1fms): %s", elapsed_ms, e
            )
            raise
