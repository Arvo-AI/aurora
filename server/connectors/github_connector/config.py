"""GitHub App configuration loaded from environment variables.

This module is intentionally env-only: it never reads from Vault and never
imports the parallel ``vault_keys`` helpers. Secrets (private key + webhook
secret) live in :mod:`connectors.github_connector.vault_keys`; this file is
strictly the *non-secret* App identity + URL config (app id, client id, webhook
URL, setup URL) plus the derived ``enabled`` flag used by the auth router and
pre-flight startup checks.

The module never raises at import time when env vars are missing — instead
:func:`load_github_app_config` returns a config with ``enabled=False`` and a
list of missing fields (via :func:`validate_github_app_config`). Callers in
the dual-mode (OAuth + App) auth router can then fall back to OAuth-only mode
without crashing the process.

Setup:
    GITHUB_APP_ID=<numeric app id>
    GITHUB_APP_CLIENT_ID=<Iv1.xxxxxxxxxxxxxxxx>
    GITHUB_APP_WEBHOOK_URL=https://<your-host>/github/webhook
    GITHUB_APP_SETUP_URL=https://<your-host>/github/app/install/callback
    # GITHUB_APP_WEBHOOK_SECRET is consumed by ``vault_keys`` as a fallback
    # when the Vault path is empty; it is NOT part of this config module.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Required env vars for App mode to be considered ``enabled``. The order is
# stable so :func:`validate_github_app_config` returns missing fields in a
# deterministic order (used by tests + startup logs).
_REQUIRED_ENV_VARS: tuple[str, ...] = (
    "GITHUB_APP_ID",
    "GITHUB_APP_CLIENT_ID",
    "GITHUB_APP_WEBHOOK_URL",
    "GITHUB_APP_SETUP_URL",
)

_cached_config: "GitHubAppConfig | None" = None


@dataclass(frozen=True)
class GitHubAppConfig:
    """Non-secret identity + URL config for the Aurora GitHub App.

    Attributes:
        app_id: Numeric GitHub App id (legacy identifier; kept for reference
            only — JWT ``iss`` MUST use ``client_id`` instead per GitHub's
            October 2024 change).
        client_id: GitHub App client id (e.g. ``Iv1.abc123...``). Used as the
            JWT ``iss`` claim by :func:`utils.auth.github_app_jwt.mint_app_jwt`.
        enabled: ``True`` only when ALL required env vars are set. When
            ``False``, the auth router skips App-mode and falls back to OAuth.
        webhook_url: Public URL GitHub posts webhook deliveries to.
        setup_url: Post-install redirect URL surfaced to users in the App
            install flow.
    """

    app_id: int
    client_id: str
    enabled: bool
    webhook_url: str
    setup_url: str


def _coerce_app_id(raw: str) -> int:
    """Parse ``GITHUB_APP_ID`` as an int, returning 0 for malformed input.

    A return of 0 keeps the dataclass constructable (so callers always get a
    value to inspect) while ``validate_github_app_config`` separately reports
    the missing/invalid env var.
    """

    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _read_env() -> dict[str, str]:
    """Snapshot the GitHub App env vars (excluding the webhook secret)."""

    return {name: (os.getenv(name) or "").strip() for name in _REQUIRED_ENV_VARS}


def validate_github_app_config() -> tuple[bool, list[str]]:
    """Return ``(ok, missing_fields)`` for startup pre-flight checks.

    ``missing_fields`` lists the env var names whose values are empty/unset,
    in the canonical declaration order from :data:`_REQUIRED_ENV_VARS`.
    Designed to be called from :mod:`main_compute` at startup so operators
    see exactly which env vars to populate to enable App mode.
    """

    env = _read_env()
    missing: list[str] = [name for name in _REQUIRED_ENV_VARS if not env[name]]

    if not missing and _coerce_app_id(env["GITHUB_APP_ID"]) <= 0:
        # Treat a present-but-non-numeric GITHUB_APP_ID as missing so the
        # operator notices the misconfiguration instead of silently running
        # with app_id=0.
        missing.append("GITHUB_APP_ID")

    return (len(missing) == 0, missing)


def load_github_app_config() -> GitHubAppConfig:
    """Load the GitHub App config from env vars (process-cached).

    Subsequent calls return the cached instance. Use :func:`clear_config_cache`
    in tests when env vars change between cases.

    Returns:
        A fully-populated :class:`GitHubAppConfig`. ``enabled`` reflects
        whether the App can actually be used (all required env vars present).
        Empty-string defaults are used for missing fields so dataclass access
        never raises — pair with :func:`validate_github_app_config` for an
        explicit missing-fields list.
    """

    global _cached_config
    if _cached_config is not None:
        return _cached_config

    env = _read_env()
    ok, _missing = validate_github_app_config()

    _cached_config = GitHubAppConfig(
        app_id=_coerce_app_id(env["GITHUB_APP_ID"]),
        client_id=env["GITHUB_APP_CLIENT_ID"],
        enabled=ok,
        webhook_url=env["GITHUB_APP_WEBHOOK_URL"],
        setup_url=env["GITHUB_APP_SETUP_URL"],
    )

    if not ok:
        logger.info(
            "[GITHUB-APP-CONFIG] disabled (missing env vars); falling back to OAuth-only mode"
        )
    else:
        logger.info(
            "[GITHUB-APP-CONFIG] enabled (client_id=%s)",
            _cached_config.client_id,
        )

    return _cached_config


def clear_config_cache() -> None:
    """Drop the cached config (test-only helper)."""

    global _cached_config
    _cached_config = None
