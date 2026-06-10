"""GitHub App secret helpers with secrets-backend + env var fallback.

When the secrets backend (Vault or AWS SM) is available, secrets are read from
it first. If the backend is unavailable or the lookup fails, environment
variables are checked as a fallback. This makes the module compatible with any
configured SECRETS_BACKEND.

Setup (Vault):
  vault kv put aurora/system/github-app/private-key value=@/path/to/github-app-private-key.pem
  vault kv put aurora/system/github-app/webhook-secret value='your-github-app-webhook-secret'

Setup (env var — required when SECRETS_BACKEND != vault):
  GITHUB_APP_PRIVATE_KEY=<PEM contents>
  GITHUB_APP_WEBHOOK_SECRET=<secret>
"""

import logging
import os

from utils.secrets import get_secrets_backend  # pyright: ignore[reportImplicitRelativeImport]

logger = logging.getLogger(__name__)

_PRIVATE_KEY_SECRET_REF = "vault:kv/data/aurora/system/github-app/private-key"
_WEBHOOK_SECRET_SECRET_REF = "vault:kv/data/aurora/system/github-app/webhook-secret"
_PRIVATE_KEY_ENV_VARS = (
    "GITHUB_APP_PRIVATE_KEY",
)
_WEBHOOK_SECRET_ENV_VARS = (
    "GITHUB_APP_WEBHOOK_SECRET",
    "GH_APP_WEBHOOK_SECRET",
    "GITHUB_WEBHOOK_SECRET",
)

_cached_private_key: str | None = None
_cached_webhook_secret: str | None = None


class GitHubAppConfigError(RuntimeError):
    """Raised when GitHub App secrets configuration is invalid."""


def clear_cache() -> None:
    """Clear cached GitHub App secrets (useful for tests)."""
    global _cached_private_key, _cached_webhook_secret
    _cached_private_key = None
    _cached_webhook_secret = None


def _read_from_backend(secret_ref: str, *, secret_label: str) -> str:
    backend = get_secrets_backend()

    if not backend.is_available():
        raise GitHubAppConfigError(
            f"Secrets backend is unavailable while reading GitHub App {secret_label}."
        )

    if not backend.can_handle_ref(secret_ref):
        raise GitHubAppConfigError(
            f"Active secrets backend cannot handle reference for GitHub App {secret_label}."
        )

    try:
        secret_value = backend.get_secret(secret_ref)
    except GitHubAppConfigError:
        raise
    except Exception as exc:
        raise GitHubAppConfigError(
            f"Failed to read GitHub App {secret_label} from secrets backend."
        ) from exc

    if not secret_value:
        raise GitHubAppConfigError(
            f"GitHub App {secret_label} is empty in secrets backend."
        )

    return secret_value


def _read_webhook_secret_from_env() -> str:
    for env_var in _WEBHOOK_SECRET_ENV_VARS:
        value = os.getenv(env_var)
        if value:
            logger.info("Using GitHub App webhook secret from environment fallback")
            return value

    raise GitHubAppConfigError(
        "GitHub App webhook secret is not configured in secrets backend or environment."
    )


def _read_private_key_from_env() -> str:
    for env_var in _PRIVATE_KEY_ENV_VARS:
        value = os.getenv(env_var)
        if value:
            value = value.replace("\\n", "\n")
            logger.info("Using GitHub App private key from environment variable")
            return value

    raise GitHubAppConfigError(
        "GitHub App private key is not configured in secrets backend or environment."
    )


def get_app_private_key() -> str:
    """Return the GitHub App private key from secrets backend, with env fallback."""
    global _cached_private_key

    if _cached_private_key is not None:
        return _cached_private_key

    try:
        _cached_private_key = _read_from_backend(
            _PRIVATE_KEY_SECRET_REF,
            secret_label="private key",
        )
        return _cached_private_key
    except GitHubAppConfigError:
        logger.debug(
            "GitHub App private key not available from secrets backend; trying environment fallback.",
        )

    _cached_private_key = _read_private_key_from_env()
    return _cached_private_key


def get_app_webhook_secret() -> str:
    """Return the GitHub App webhook secret from secrets backend, with env fallback."""
    global _cached_webhook_secret

    if _cached_webhook_secret is not None:
        return _cached_webhook_secret

    try:
        _cached_webhook_secret = _read_from_backend(
            _WEBHOOK_SECRET_SECRET_REF,
            secret_label="webhook secret",
        )
        return _cached_webhook_secret
    except GitHubAppConfigError:
        logger.debug(
            "GitHub App webhook secret not available from secrets backend; trying environment fallback.",
        )

    _cached_webhook_secret = _read_webhook_secret_from_env()
    return _cached_webhook_secret
