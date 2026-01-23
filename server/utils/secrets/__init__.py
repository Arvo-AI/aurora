"""
Secrets management module using HashiCorp Vault.

This module provides a unified interface for secrets storage using Vault.
"""

import logging
from typing import Optional

from .base import SecretsBackend

logger = logging.getLogger(__name__)

# Module-level singleton for the backend instance
_backend_instance: Optional[SecretsBackend] = None


def get_secrets_backend() -> SecretsBackend:
    """Get the Vault secrets backend singleton.

    Returns:
        VaultSecretsBackend instance
    """
    global _backend_instance

    if _backend_instance is not None:
        return _backend_instance

    from .vault_backend import VaultSecretsBackend

    _backend_instance = VaultSecretsBackend()
    logger.info("Secrets backend: HashiCorp Vault")

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
