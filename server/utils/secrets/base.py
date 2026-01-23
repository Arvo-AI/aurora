"""
Abstract interface for secrets storage backends.

This module defines the contract that all secrets backends must implement,
enabling provider-agnostic secrets management.
"""

from abc import ABC, abstractmethod
from typing import Optional


class SecretsBackend(ABC):
    """Abstract base class for secrets storage backends.

    Implementations must provide methods for storing, retrieving, and deleting
    secrets. Each backend returns references in its own format which can be
    used to retrieve the secret later.

    Reference format:
    - Vault: vault:kv/data/{mount}/users/{secret_name}
    """

    @abstractmethod
    def store_secret(self, secret_name: str, secret_value: str, **kwargs) -> str:
        """Store a secret and return a reference string.

        Args:
            secret_name: Unique identifier for the secret
            secret_value: The secret data to store
            **kwargs: Backend-specific options (e.g., project_id for GCP)

        Returns:
            A reference string that can be used to retrieve the secret

        Raises:
            Exception: If storage fails
        """
        pass

    @abstractmethod
    def get_secret(self, secret_ref: str) -> str:
        """Retrieve a secret by its reference.

        Args:
            secret_ref: Reference string returned by store_secret()

        Returns:
            The secret value as a string

        Raises:
            Exception: If retrieval fails or secret not found
        """
        pass

    @abstractmethod
    def delete_secret(self, secret_ref: str) -> None:
        """Delete a secret by its reference.

        Args:
            secret_ref: Reference string returned by store_secret()

        Raises:
            RuntimeError: If the backend is not available
            ValueError: If the secret reference format is invalid
            Exception: If deletion fails
        """
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """Check if the backend is configured and available.

        Returns:
            True if the backend can be used, False otherwise
        """
        pass

    def can_handle_ref(self, secret_ref: str) -> bool:
        """Check if this backend can handle the given secret reference.

        Used to determine which backend should retrieve a secret based on
        its reference format.

        Args:
            secret_ref: A secret reference string

        Returns:
            True if this backend can handle the reference
        """
        return False
