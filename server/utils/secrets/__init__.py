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

# Shown to users in place of a provider-specific credential error when the
# secrets backend itself is down. Every provider's credentials live behind it, so
# an outage makes them all fail at once, which otherwise reads as a cloud
# authentication problem and sends people re-adding connectors that never broke.
_UNAVAILABLE_PREAMBLE = (
    "Secrets backend ({name}) is unavailable, so stored credentials could not be "
    "read. This is not a problem with your cloud credentials — "
)

SECRETS_BACKEND_UNAVAILABLE_MESSAGE = _UNAVAILABLE_PREAMBLE.format(name="Vault") + (
    "check VAULT_ADDR, VAULT_TOKEN and the Vault seal status."
)

AWS_SM_UNAVAILABLE_MESSAGE = _UNAVAILABLE_PREAMBLE.format(
    name="AWS Secrets Manager"
) + "check AWS_SM_REGION and the Secrets Manager credentials."


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


def unavailable_backend_message(backend: SecretsBackend) -> str:
    """Recovery guidance naming the backend that is actually configured.

    ``SECRETS_BACKEND=aws_secrets_manager`` with a missing ``AWS_SM_REGION`` is also
    an unavailable backend, and pointing that operator at ``VAULT_ADDR`` sends them
    to a service they aren't running.
    """
    from .aws_sm_backend import AWSSecretsManagerBackend

    if isinstance(backend, AWSSecretsManagerBackend):
        return AWS_SM_UNAVAILABLE_MESSAGE
    return SECRETS_BACKEND_UNAVAILABLE_MESSAGE


def credential_error_message(fallback: str) -> str:
    """Return the message to show when a credential lookup produced nothing.

    Distinguishes "the secrets backend is down" from "these credentials are
    wrong/missing". Callers pass their own provider-specific wording as
    ``fallback``; it is returned unchanged whenever the backend is healthy, so a
    genuine credential problem still reads as one.
    """
    try:
        backend = get_secrets_backend()
        if not backend.is_available():
            return unavailable_backend_message(backend)
    # The diagnostic must never replace the error it is annotating.
    except Exception as e:
        logger.warning("Could not check secrets backend availability: %s", e)
    return fallback


__all__ = [
    "SecretsBackend",
    "get_secrets_backend",
    "reset_backend",
    "credential_error_message",
    "unavailable_backend_message",
    "SECRETS_BACKEND_UNAVAILABLE_MESSAGE",
    "AWS_SM_UNAVAILABLE_MESSAGE",
]
