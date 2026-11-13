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

Either secret can instead come from an environment variable, so a deployment
can provide it through its own secret store (for example a Kubernetes Secret)
without writing to the secrets backend:

* webhook secret: ``GITHUB_APP_WEBHOOK_SECRET`` / ``GH_APP_WEBHOOK_SECRET`` /
  ``GITHUB_WEBHOOK_SECRET`` (first non-blank one wins)
* private key: ``GITHUB_APP_PRIVATE_KEY``, as the PEM text (real or ``\\n``-escaped
  newlines) or as the base64 of the PEM file (wrapped or not)

Precedence: set each secret in one place; the environment wins if set. When the
env var is set (non-blank) it is used and the secrets backend is never
consulted. The backend is read only when the env var is not set. Either way the
value is read once and cached for the life of the process: an env var cannot
change while the process runs, so rotating a key means restarting the pods. The
source is logged once, at INFO, the first time each secret is read; the value
is never logged.
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

# Resolved secrets, cached for the life of the process whichever source they
# came from. Filled with dict.setdefault so that when request threads race on
# the first read, one value wins and only the winner logs its source.
_PRIVATE_KEY_CACHE_KEY = "private_key"
_WEBHOOK_SECRET_CACHE_KEY = "webhook_secret"
_cache: dict[str, str] = {}


class GitHubAppConfigError(RuntimeError):
    """Raised when GitHub App secrets configuration is invalid."""


def clear_cache() -> None:
    """Clear cached GitHub App secrets (useful for tests)."""
    _cache.clear()


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
        # surfaces as GitHubAppConfigError, the only error type callers handle.
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


def _env_value(env_var: str) -> str | None:
    """The env var's value, or None when it is unset or blank."""
    value = os.getenv(env_var)
    if value and value.strip():
        return value
    return None


def _webhook_secret_from_env() -> tuple[str, str] | None:
    """(env var name, value) of the first webhook secret env var that is set."""
    for env_var in _WEBHOOK_SECRET_ENV_VARS:
        value = _env_value(env_var)
        if value is not None:
            return env_var, value
    return None


def _read_backend_or_explain(logical_name: str, *, secret_label: str, env_hint: str) -> str:
    """Read from the backend; on failure, say the env var is the alternative."""
    try:
        return _read_system_secret(logical_name, secret_label=secret_label)
    except GitHubAppConfigError as exc:
        raise GitHubAppConfigError(
            f"{exc} Store it in the secrets backend or set {env_hint}."
        ) from exc


def _cache_once(cache_key: str, value: str, *, secret_label: str, source: str) -> str:
    """Cache the value; the first writer logs where it came from (never the value)."""
    cached = _cache.setdefault(cache_key, value)
    if cached is value:
        logger.info("GitHub App %s loaded from %s", secret_label, source)
    return cached


def get_app_private_key() -> str:
    """Return the GitHub App private key: env var if set, else the backend."""
    cached = _cache.get(_PRIVATE_KEY_CACHE_KEY)
    if cached is not None:
        return cached

    raw = _env_value(_PRIVATE_KEY_ENV_VAR)
    if raw is not None:
        value = _normalize_private_key(raw)
        source = f"environment ({_PRIVATE_KEY_ENV_VAR})"
    else:
        value = _read_backend_or_explain(
            _PRIVATE_KEY_LOGICAL_NAME,
            secret_label="private key",
            env_hint=_PRIVATE_KEY_ENV_VAR,
        )
        source = "secrets backend"
    return _cache_once(
        _PRIVATE_KEY_CACHE_KEY, value, secret_label="private key", source=source
    )


def get_app_webhook_secret() -> str:
    """Return the GitHub App webhook secret: env var if set, else the backend."""
    cached = _cache.get(_WEBHOOK_SECRET_CACHE_KEY)
    if cached is not None:
        return cached

    from_env = _webhook_secret_from_env()
    if from_env is not None:
        env_var, value = from_env
        source = f"environment ({env_var})"
    else:
        value = _read_backend_or_explain(
            _WEBHOOK_SECRET_LOGICAL_NAME,
            secret_label="webhook secret",
            env_hint=_WEBHOOK_SECRET_ENV_VARS[0],
        )
        source = "secrets backend"
    return _cache_once(
        _WEBHOOK_SECRET_CACHE_KEY, value, secret_label="webhook secret", source=source
    )
