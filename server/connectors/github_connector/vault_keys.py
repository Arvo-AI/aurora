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

The secrets backend is always tried first. A value read from the backend is
cached for the life of the process; an environment fallback is cached only for
``_ENV_FALLBACK_TTL_SECONDS``, so a backend outage at startup does not pin a
stale env value after the backend recovers (e.g. after a key rotation).
"""

import base64
import binascii
import logging
import os
import time

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

# How long an env-fallback value is reused before the backend is retried.
_ENV_FALLBACK_TTL_SECONDS = 60.0

# A cache entry is (value, expires_at). expires_at is a monotonic deadline for
# an env-fallback value, or None for a backend value, which does not expire.
# Each entry is replaced by one item assignment so concurrent request threads
# never pair a value with another value's expiry.
_CacheEntry = tuple[str, float | None]

_PRIVATE_KEY_CACHE_KEY = "private_key"
_WEBHOOK_SECRET_CACHE_KEY = "webhook_secret"
_cache: dict[str, _CacheEntry] = {}


class GitHubAppConfigError(RuntimeError):
    """Raised when GitHub App secrets configuration is invalid."""


def clear_cache() -> None:
    """Clear cached GitHub App secrets (useful for tests)."""
    _cache.clear()


def _fresh_value(entry: _CacheEntry | None) -> str | None:
    """Return the cached value unless it is missing or its TTL has passed."""
    if entry is None:
        return None
    value, expires_at = entry
    if expires_at is None or time.monotonic() < expires_at:
        return value
    return None


def _env_fallback_entry(value: str) -> _CacheEntry:
    return (value, time.monotonic() + _ENV_FALLBACK_TTL_SECONDS)


def _read_system_secret(logical_name: str, *, secret_label: str) -> str:
    """Read a system secret from the active backend (Vault or AWS SM)."""
    try:
        backend = get_secrets_backend()
    except Exception as exc:
        raise GitHubAppConfigError(
            f"Secrets backend is misconfigured while reading GitHub App {secret_label}."
        ) from exc

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


def _decode_private_key_text(raw: str) -> str:
    """Accept PEM text (real or escaped newlines) or base64 of the PEM file."""
    value = raw.strip()
    if _PEM_MARKER in value:
        return value.replace("\\n", "\n")
    # `base64 key.pem` wraps at 76 columns; drop all whitespace before the
    # strict decode so wrapped output is accepted.
    compact = "".join(value.split())
    try:
        decoded = base64.b64decode(compact, validate=True).decode("utf-8").strip()
    except (binascii.Error, ValueError):
        decoded = ""
    if _PEM_MARKER in decoded:
        return decoded
    raise GitHubAppConfigError(
        f"{_PRIVATE_KEY_ENV_VAR} is set but is neither a PEM key nor base64 of one."
    )


def _normalize_private_key(raw: str) -> str:
    """Decode the env value and check it parses as a private key.

    PyJWT signs the App JWT with ``cryptography``'s PEM loader, so loading the
    key here catches a truncated or non-private PEM at startup instead of on
    the first GitHub call.
    """
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    pem = _decode_private_key_text(raw)
    try:
        load_pem_private_key(pem.encode("utf-8"), password=None)
    except (ValueError, TypeError) as exc:
        raise GitHubAppConfigError(
            f"{_PRIVATE_KEY_ENV_VAR} does not contain a valid unencrypted PEM private key."
        ) from exc
    return pem


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
    cached = _fresh_value(_cache.get(_PRIVATE_KEY_CACHE_KEY))
    if cached is not None:
        return cached

    try:
        value = _read_system_secret(
            _PRIVATE_KEY_LOGICAL_NAME,
            secret_label="private key",
        )
        _cache[_PRIVATE_KEY_CACHE_KEY] = (value, None)
        return value
    except GitHubAppConfigError as backend_error:
        if not os.getenv(_PRIVATE_KEY_ENV_VAR, "").strip():
            raise
        logger.warning(
            "GitHub App private key lookup from secrets backend failed; "
            "using environment fallback (%s)",
            backend_error,
        )

    value = _read_private_key_from_env()
    _cache[_PRIVATE_KEY_CACHE_KEY] = _env_fallback_entry(value)
    return value


def get_app_webhook_secret() -> str:
    """Return the GitHub App webhook secret from the backend, with env fallback."""
    cached = _fresh_value(_cache.get(_WEBHOOK_SECRET_CACHE_KEY))
    if cached is not None:
        return cached

    try:
        value = _read_system_secret(
            _WEBHOOK_SECRET_LOGICAL_NAME,
            secret_label="webhook secret",
        )
        _cache[_WEBHOOK_SECRET_CACHE_KEY] = (value, None)
        return value
    except GitHubAppConfigError as backend_error:
        logger.warning(
            "GitHub App webhook secret lookup from secrets backend failed; "
            "trying environment fallback (%s)",
            type(backend_error).__name__,
        )

    value = _read_webhook_secret_from_env()
    _cache[_WEBHOOK_SECRET_CACHE_KEY] = _env_fallback_entry(value)
    return value
