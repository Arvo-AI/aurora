"""GitHub App secret helpers backed by Vault.

Setup:
  vault kv put aurora/system/github-app/private-key value=@/path/to/github-app-private-key.pem
  vault kv put aurora/system/github-app/webhook-secret value='your-github-app-webhook-secret'
"""

import logging
import os

from utils.secrets import get_secrets_backend  # pyright: ignore[reportImplicitRelativeImport]

logger = logging.getLogger(__name__)

_PRIVATE_KEY_SECRET_REF = "vault:kv/data/aurora/system/github-app/private-key"
_WEBHOOK_SECRET_SECRET_REF = "vault:kv/data/aurora/system/github-app/webhook-secret"
_WEBHOOK_SECRET_ENV_VARS = (
    "GITHUB_APP_WEBHOOK_SECRET",
    "GH_APP_WEBHOOK_SECRET",
    "GITHUB_WEBHOOK_SECRET",
)

_cached_private_key: str | None = None
_cached_webhook_secret: str | None = None


class GitHubAppConfigError(RuntimeError):
    """Raised when GitHub App Vault or environment configuration is invalid."""


def clear_cache() -> None:
    """Clear cached GitHub App secrets (useful for tests)."""
    global _cached_private_key, _cached_webhook_secret
    _cached_private_key = None
    _cached_webhook_secret = None


def _read_vault_secret(secret_ref: str, *, secret_label: str) -> str:
    backend = get_secrets_backend()

    if not backend.is_available():
        raise GitHubAppConfigError(
            f"Vault secrets backend is unavailable while reading GitHub App {secret_label}."
        )

    try:
        secret_value = backend.get_secret(secret_ref)
    except GitHubAppConfigError:
        raise
    except Exception as exc:
        raise GitHubAppConfigError(
            f"Failed to read GitHub App {secret_label} from Vault."
        ) from exc

    if not secret_value:
        raise GitHubAppConfigError(
            f"GitHub App {secret_label} is empty in Vault."
        )

    return secret_value


def _read_webhook_secret_from_env() -> str:
    for env_var in _WEBHOOK_SECRET_ENV_VARS:
        value = os.getenv(env_var)
        if value:
            logger.info("Using GitHub App webhook secret from %s fallback", env_var)
            return value

    raise GitHubAppConfigError(
        "GitHub App webhook secret is not configured in Vault or environment."
    )


def get_app_private_key() -> str:
    """Return the GitHub App private key from Vault."""
    global _cached_private_key

    if _cached_private_key is not None:
        return _cached_private_key

    _cached_private_key = _read_vault_secret(
        _PRIVATE_KEY_SECRET_REF,
        secret_label="private key",
    )
    return _cached_private_key


def get_app_webhook_secret() -> str:
    """Return the GitHub App webhook secret from Vault, with env fallback."""
    global _cached_webhook_secret

    if _cached_webhook_secret is not None:
        return _cached_webhook_secret

    try:
        _cached_webhook_secret = _read_vault_secret(
            _WEBHOOK_SECRET_SECRET_REF,
            secret_label="webhook secret",
        )
        return _cached_webhook_secret
    except GitHubAppConfigError as vault_error:
        logger.warning(
            "GitHub App webhook secret lookup from Vault failed; trying environment fallback (%s)",
            type(vault_error).__name__,
        )

    _cached_webhook_secret = _read_webhook_secret_from_env()
    return _cached_webhook_secret
