"""
Secrets management module.

This module provides a unified interface for secrets storage.
The backend is selected via the SECRETS_BACKEND environment variable:
- "vault" (default): HashiCorp Vault
- "aws_secrets_manager": AWS Secrets Manager
"""

import os
import logging
import threading
from typing import Optional

from .base import SecretsBackend

logger = logging.getLogger(__name__)

_backend_instance: Optional[SecretsBackend] = None
_backend_lock = threading.Lock()


def get_secrets_backend() -> SecretsBackend:
    """Get the configured secrets backend singleton.

    Backend is selected via the SECRETS_BACKEND environment variable.
    Thread-safe via _backend_lock.

    Returns:
        SecretsBackend instance (Vault or AWS Secrets Manager)
    """
    global _backend_instance

    if _backend_instance is not None:
        return _backend_instance

    with _backend_lock:
        if _backend_instance is not None:
            return _backend_instance

        backend = os.getenv("SECRETS_BACKEND", "vault")

        if backend == "vault":
            from .vault_backend import VaultSecretsBackend

            _backend_instance = VaultSecretsBackend()
            logger.info("Secrets backend: HashiCorp Vault")
        elif backend == "aws_secrets_manager":
            from .aws_sm_backend import AWSSecretsManagerBackend

            _backend_instance = AWSSecretsManagerBackend()
            logger.info("Secrets backend: AWS Secrets Manager")
        else:
            raise ValueError(
                f"Unknown SECRETS_BACKEND: '{backend}'. "
                "Supported values: 'vault', 'aws_secrets_manager'"
            )

    return _backend_instance


def reset_backend():
    """Reset the backend singleton (primarily for testing).

    This allows tests to switch backends between test cases.
    """
    global _backend_instance
    _backend_instance = None


__all__ = [
    "SecretsBackend",
    "get_secrets_backend",
    "reset_backend",
]
