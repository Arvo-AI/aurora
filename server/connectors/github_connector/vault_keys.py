"""GitHub App secret helpers backed by the active secrets backend.

The GitHub App private key + webhook secret are *system-level*,
operator-provisioned, read-only secrets. They are read from whichever secrets
backend is configured via ``SECRETS_BACKEND`` (HashiCorp Vault or AWS Secrets
Manager) — the reference is built by the active backend's ``build_system_ref``
so nothing is hardcoded to a single backend.

Setup (Vault):
  vault kv put aurora/system/github-app/private-key value=@/path/to/github-app-private-key.pem
  vault kv put aurora/system/github-app/webhook-secret value='your-github-app-webhook-secret'

Setup (AWS Secrets Manager):
  aws secretsmanager create-secret --name aurora/system/github-app/private-key \
    --secret-string file:///path/to/github-app-private-key.pem --region "$AWS_SM_REGION"
  aws secretsmanager create-secret --name aurora/system/github-app/webhook-secret \
    --secret-string 'your-github-app-webhook-secret' --region "$AWS_SM_REGION"

Both secrets fall back to environment variables when the backend cannot
supply them, so a deployment can provide them through its own secret store
(for example a Kubernetes Secret) without writing to Vault:

* webhook secret: ``GITHUB_APP_WEBHOOK_SECRET`` / ``GH_APP_WEBHOOK_SECRET`` /
  ``GITHUB_WEBHOOK_SECRET``
* private key: ``GITHUB_APP_PRIVATE_KEY``, as the PEM text (real or ``\\n``-escaped
  newlines) or as the base64 of the PEM file

The secrets backend is always tried first.
"""

import base64
import binascii
import logging
import os

from utils.secrets import get_secrets_backend  # pyright: ignore[reportImplicitRelativeImport]

logger = logging.getLogger(__name__)

# Backend-agnostic logical names; the active backend maps these to a concrete
# reference via build_system_ref (e.g. vault:kv/data/aurora/system/... or
# awssm:{region}:aurora/system/...).
_PRIVATE_KEY_LOGICAL_NAME = "github-app/private-key"
_WEBHOOK_SECRET_LOGICAL_NAME = "github-app/webhook-secret"
_PRIVATE_KEY_ENV_VAR = "GITHUB_APP_PRIVATE_KEY"
_PEM_MARKER = "-----BEGIN"
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


def _read_system_secret(logical_name: str, *, secret_label: str) -> str:
    """Read a system secret from the active backend (Vault or AWS SM)."""
    backend = get_secrets_backend()

    if not backend.is_available():
        raise GitHubAppConfigError(
            f"Secrets backend is unavailable while reading GitHub App {secret_label}."
        )

    secret_ref = None
    try:
        # build_system_ref is inside the try so a backend that doesn't
        # implement it (NotImplementedError) — or any other unexpected error —
        # surfaces as GitHubAppConfigError. get_app_webhook_secret's env
        # fallback relies on this being the only exception type raised here.
        secret_ref = backend.build_system_ref(logical_name)
        secret_value = backend.get_secret(secret_ref)
    except GitHubAppConfigError:
        raise
    except Exception as exc:
        message = f"Failed to read GitHub App {secret_label} from secrets backend."
        if _is_permission_denied(exc):
            message += (
                " The backend denied access: the credentials Aurora uses need read"
                f" access to {secret_ref or 'the system secrets path'}."
            )
        raise GitHubAppConfigError(message) from exc

    if not secret_value:
        raise GitHubAppConfigError(
            f"GitHub App {secret_label} is empty in secrets backend."
        )

    return secret_value


def _is_permission_denied(exc: BaseException) -> bool:
    """True when the backend refused access (Vault 403, AWS AccessDenied)."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__name__ in ("Forbidden", "AccessDeniedException"):
            return True
        response = getattr(current, "response", None)
        if isinstance(response, dict) and response.get("Error", {}).get("Code") == "AccessDeniedException":
            return True
        current = current.__cause__ or current.__context__
    return False


def _normalize_private_key(raw: str) -> str:
    """Accept PEM text (real or escaped newlines) or base64 of the PEM file."""
    value = raw.strip()
    if _PEM_MARKER in value:
        return value.replace("\\n", "\n")
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8").strip()
    except (binascii.Error, ValueError):
        decoded = ""
    if _PEM_MARKER in decoded:
        return decoded
    raise GitHubAppConfigError(
        f"{_PRIVATE_KEY_ENV_VAR} is set but is neither a PEM key nor base64 of one."
    )


def _read_private_key_from_env() -> str:
    value = os.getenv(_PRIVATE_KEY_ENV_VAR)
    if not value or not value.strip():
        raise GitHubAppConfigError(
            "GitHub App private key is not configured in secrets backend or environment."
        )
    logger.info("Using GitHub App private key from environment fallback")
    return _normalize_private_key(value)


def _read_webhook_secret_from_env() -> str:
    for env_var in _WEBHOOK_SECRET_ENV_VARS:
        value = os.getenv(env_var)
        if value:
            logger.info("Using GitHub App webhook secret from environment fallback")
            return value

    raise GitHubAppConfigError(
        "GitHub App webhook secret is not configured in secrets backend or environment."
    )


def get_app_private_key() -> str:
    """Return the GitHub App private key from the backend, with env fallback."""
    global _cached_private_key

    if _cached_private_key is not None:
        return _cached_private_key

    try:
        _cached_private_key = _read_system_secret(
            _PRIVATE_KEY_LOGICAL_NAME,
            secret_label="private key",
        )
        return _cached_private_key
    except GitHubAppConfigError as backend_error:
        if not os.getenv(_PRIVATE_KEY_ENV_VAR, "").strip():
            raise
        logger.warning(
            "GitHub App private key lookup from secrets backend failed; "
            "using environment fallback (%s)",
            backend_error,
        )

    _cached_private_key = _read_private_key_from_env()
    return _cached_private_key


def get_app_webhook_secret() -> str:
    """Return the GitHub App webhook secret from the backend, with env fallback."""
    global _cached_webhook_secret

    if _cached_webhook_secret is not None:
        return _cached_webhook_secret

    try:
        _cached_webhook_secret = _read_system_secret(
            _WEBHOOK_SECRET_LOGICAL_NAME,
            secret_label="webhook secret",
        )
        return _cached_webhook_secret
    except GitHubAppConfigError as backend_error:
        logger.warning(
            "GitHub App webhook secret lookup from secrets backend failed; "
            "trying environment fallback (%s)",
            type(backend_error).__name__,
        )

    _cached_webhook_secret = _read_webhook_secret_from_env()
    return _cached_webhook_secret
