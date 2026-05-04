"""GitHub App installation token cache with per-installation refresh lock.

This module mints and caches installation access tokens for the Aurora
GitHub App. Each token is short-lived (~1 hour from GitHub) and is
auto-refreshed when its remaining lifetime falls below 60 seconds.

Concurrency
-----------
Aurora runs on Flask + Celery (threading model, NO asyncio anywhere). To
prevent multiple workers from minting tokens for the same installation
simultaneously (a "thundering herd" against GitHub's API which would
trigger secondary rate limits), refresh is serialised per installation
via the lock-of-locks pattern:

- A single, fast ``_lock_creation_lock`` (``threading.Lock``) protects
  only the dict-mutation that adds new entries to
  ``_per_installation_locks``. It is never held across an HTTP call.
- Once a per-installation lock exists, callers acquire it directly
  (the fast path, after a cache hit, never touches the global lock).
- Inside the per-installation lock, we re-check the cache before
  minting (double-checked locking) so workers that lost the race do
  not also mint a token.

Future work (documented per plan task 8): this is intentionally an
in-process cache. Multi-worker deployments will mint up to one token
per worker per refresh window. For Aurora's current Celery + Flask
scale that is acceptable; a Redis-backed shared cache + Redlock would
be the natural upgrade path when worker count grows beyond the current
single-host deployment.

Security
--------
- Token values are NEVER logged at any level.
- Error paths redact any ``ghs_<...>`` substring before including the
  response body in the typed exception message, so even an attacker
  who can read logs cannot reconstruct a leaked token.
- ``Authorization`` headers are constructed inline and never echoed.

Standard log keys
-----------------
This module emits structured ``key=value`` log lines on the canonical
key ``gh_app_token_event``. The known event values are:

    * ``mint_attempt`` — minting started; emitted before the HTTP call.
    * ``mint_success`` — minting succeeded; ``duration_ms`` populated.
    * ``mint_failed``  — minting failed; ``error_class`` populated.
    * ``cache_hit``    — token returned from cache without minting;
      ``cached_age_seconds`` populated.

Other keys present on these lines:

    * ``installation_id``    — GitHub installation id (always present).
    * ``duration_ms``        — wall-clock elapsed for the mint call.
    * ``cached_age_seconds`` — age of the cached token (cache_hit only).
    * ``error_class``        — exception class name (mint_failed only).

Token values are NEVER logged. Any exception body that may include
token-shaped substrings is passed through ``redact_token()`` before
inclusion in any log line.

Reference: https://docs.github.com/en/rest/apps/apps#create-an-installation-access-token-for-an-app
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timezone

import requests

from utils.auth.github_app_jwt import mint_app_jwt
from utils.auth.log_redact import redact_token

logger = logging.getLogger(__name__)

# 20s timeout matches the project-wide ``GITHUB_TIMEOUT`` constant in
# ``server/routes/github/github.py`` for consistency across all GitHub
# HTTP calls.
_GITHUB_TIMEOUT_SECONDS = 20

# Refresh tokens whose remaining lifetime falls below this many seconds.
# GitHub installation tokens last ~1 hour; a 60s buffer absorbs network
# round-trip + clock skew so we never hand out a token that expires
# mid-request.
_REFRESH_BUFFER_SECONDS = 60

# Pattern used to redact token-like substrings from any response body
# we include in error messages. GitHub installation tokens start with
# ``ghs_`` followed by alphanumeric/underscore characters.
_TOKEN_PATTERN = re.compile(r"ghs_[A-Za-z0-9_]+")

# Module-level state. ``_token_cache`` mutations happen under a per-
# installation lock; ``_per_installation_locks`` mutations happen under
# the global ``_lock_creation_lock`` (and only for inserts, never
# across an HTTP call). ``_token_cache_minted_at`` mirrors
# ``_token_cache`` and stores the unix timestamp at which the cached
# token was added — read-only outside the same lock scope, only used
# to compute ``cached_age_seconds`` for the ``cache_hit`` log line.
_token_cache: dict[int, tuple[str, int]] = {}
_token_cache_minted_at: dict[int, int] = {}
_per_installation_locks: dict[int, threading.Lock] = {}
_lock_creation_lock = threading.Lock()


class GitHubAppTokenError(Exception):
    """Raised on generic 4xx/5xx errors from the install-token endpoint.

    More specific subclasses (:class:`GitHubAppInstallationNotFound`,
    :class:`GitHubAppInstallationSuspended`) are raised for the cases
    where a caller can take a structured action (mark deleted /
    surface suspended-state UI). Plain ``GitHubAppTokenError`` covers
    network failures, malformed responses, and any other 4xx/5xx that
    does not match a specialised handler.
    """


class GitHubAppInstallationNotFound(GitHubAppTokenError):
    """Raised on 404 — the installation_id no longer exists at GitHub.

    Caller should mark the corresponding ``github_installations`` row
    as deleted (or remove the user link) so subsequent auth-router
    lookups fall back to OAuth.
    """


class GitHubAppInstallationSuspended(GitHubAppTokenError):
    """Raised on 403 with a body indicating the installation was suspended.

    Caller should set ``suspended_at`` on the installation row, surface
    the suspended state in UI, and (per the auth-router design) fall
    back to OAuth when an OAuth credential is available for the user.
    """


def _get_per_installation_lock(installation_id: int) -> threading.Lock:
    """Return the per-installation lock, creating it on first use.

    The per-installation lock is acquired during the (slow) HTTP token
    mint. The ``_lock_creation_lock`` is only held briefly while we
    insert a new entry into ``_per_installation_locks`` — it is never
    held across an HTTP call, so a slow GitHub response on installation
    A cannot block a refresh for installation B.
    """

    lock = _per_installation_locks.get(installation_id)
    if lock is not None:
        return lock

    with _lock_creation_lock:
        # Re-check after acquiring; another thread may have created the
        # lock between our first check and the global-lock acquisition.
        lock = _per_installation_locks.get(installation_id)
        if lock is None:
            lock = threading.Lock()
            _per_installation_locks[installation_id] = lock
    return lock


def _parse_expires_at(expires_at_iso: str) -> int:
    """Parse GitHub's ``expires_at`` ISO-8601 string to a unix timestamp.

    GitHub returns timestamps such as ``2025-01-01T00:00:00Z``. We accept
    both the ``Z`` shorthand and explicit offsets (``+00:00``) so the
    parser is tolerant of GitHub Enterprise variants and test fixtures.
    """

    text = expires_at_iso.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def _redact_tokens(text: str) -> str:
    """Replace any ``ghs_...`` substring with ``ghs_<REDACTED>``."""

    return _TOKEN_PATTERN.sub("ghs_<REDACTED>", text)


def _cache_is_fresh(cached: tuple[str, int] | None) -> bool:
    """Return True iff ``cached`` is present and not within the refresh buffer."""

    if cached is None:
        return False
    _, expires_at = cached
    return (expires_at - int(time.time())) >= _REFRESH_BUFFER_SECONDS


def _mint_token(installation_id: int) -> tuple[str, int]:
    """Hit GitHub's install-token endpoint and return ``(token, expires_at_unix)``.

    Raises typed exceptions for 404 / 403-suspended / other failures.
    The token value is never logged. Response bodies included in error
    messages are passed through :func:`_redact_tokens` so a leaked
    response cannot leak a token via the logs.
    """

    jwt_token = mint_app_jwt()
    url = f"https://api.github.com/app/installations/{installation_id}/access_tokens"
    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        response = requests.post(url, headers=headers, timeout=_GITHUB_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        # Network-level failure (DNS, connection refused, timeout). We
        # do not log the JWT or response details here. Defensive
        # ``redact_token`` on ``str(exc)`` covers the (rare) case where
        # a proxy echoes the Authorization header back into the error.
        raise GitHubAppTokenError(
            f"Failed to reach GitHub install-token endpoint for installation_id={installation_id}: {type(exc).__name__}: {redact_token(str(exc))}"
        ) from exc

    if response.status_code == 201:
        try:
            payload: dict[str, str] = response.json()
            token: str = payload["token"]
            expires_at_raw: str = payload["expires_at"]
            expires_at = _parse_expires_at(expires_at_raw)
        except (ValueError, KeyError, TypeError) as exc:
            raise GitHubAppTokenError(
                f"GitHub install-token response was malformed for installation_id={installation_id}: {type(exc).__name__}: {exc}"
            ) from exc
        return token, expires_at

    if response.status_code == 404:
        raise GitHubAppInstallationNotFound(
            f"GitHub installation_id={installation_id} not found (404)"
        )

    if response.status_code == 403:
        body_text = response.text or ""
        if "This installation has been suspended" in body_text:
            raise GitHubAppInstallationSuspended(
                f"GitHub installation_id={installation_id} is suspended"
            )
        raise GitHubAppTokenError(
            f"GitHub install-token request failed for installation_id={installation_id} (status=403): {_redact_tokens(body_text)[:200]}"
        )

    raise GitHubAppTokenError(
        f"GitHub install-token request failed for installation_id={installation_id} (status={response.status_code}): {_redact_tokens(response.text or '')[:200]}"
    )


def get_installation_token(installation_id: int) -> str:
    """Return a valid installation access token for the given installation.

    Cached tokens are returned without re-minting until they fall within
    :data:`_REFRESH_BUFFER_SECONDS` of expiry. Concurrent requests for
    the same installation serialise on a per-installation lock so the
    GitHub API is hit at most once per refresh window even under heavy
    fan-out from Celery workers.

    Args:
        installation_id: GitHub installation id (e.g. from the install
            callback or a webhook payload).

    Returns:
        The installation access token string (``ghs_...``).

    Raises:
        GitHubAppInstallationNotFound: 404 from GitHub. Caller should
            mark the installation as deleted in the database.
        GitHubAppInstallationSuspended: 403 with the suspended marker.
            Caller should set ``suspended_at`` on the installation row
            and fall back to OAuth when available.
        GitHubAppTokenError: Any other failure (network error, 5xx,
            4xx without a more specific mapping). The error message
            redacts any token-shaped substring from the response body.
    """

    # Fast path: cache hit, no lock acquired. This is the common case
    # once a token has been minted for the installation; it must remain
    # cheap because read-heavy paths (Celery workers handling webhooks,
    # agent tools fetching repo metadata) hit this many times per token.
    cached = _token_cache.get(installation_id)
    if cached is not None and _cache_is_fresh(cached):
        _log_cache_hit(installation_id)
        return cached[0]

    lock = _get_per_installation_lock(installation_id)
    with lock:
        # Double-checked locking: another thread may have minted while
        # we were waiting on the lock. Re-check before doing the slow
        # HTTP call so 19 of the 20 racing workers get the cache hit.
        cached = _token_cache.get(installation_id)
        if cached is not None and _cache_is_fresh(cached):
            _log_cache_hit(installation_id)
            return cached[0]

        logger.info(
            "gh_app_token_event=mint_attempt installation_id=%d",
            installation_id,
        )
        start = time.monotonic()
        try:
            token, expires_at = _mint_token(installation_id)
        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            logger.warning(
                "gh_app_token_event=mint_failed installation_id=%d "
                "duration_ms=%d error_class=%s",
                installation_id,
                duration_ms,
                type(exc).__name__,
            )
            raise
        duration_ms = int((time.monotonic() - start) * 1000)
        _token_cache[installation_id] = (token, expires_at)
        _token_cache_minted_at[installation_id] = int(time.time())
        logger.info(
            "gh_app_token_event=mint_success installation_id=%d duration_ms=%d",
            installation_id,
            duration_ms,
        )
        return token


def _log_cache_hit(installation_id: int) -> None:
    """Emit the ``gh_app_token_event=cache_hit`` structured log line.

    ``cached_age_seconds`` falls back to 0 if the mint timestamp was
    not recorded (defensive — the parallel dict is always populated
    alongside ``_token_cache`` writes, but a bare-tuple poke from a
    test would not).
    """
    minted_at = _token_cache_minted_at.get(installation_id)
    cached_age_seconds = max(0, int(time.time()) - minted_at) if minted_at else 0
    logger.info(
        "gh_app_token_event=cache_hit installation_id=%d cached_age_seconds=%d",
        installation_id,
        cached_age_seconds,
    )


def clear_cache() -> None:
    """Drop all cached tokens and per-installation locks.

    Intended for tests only. Holding any per-installation lock while
    this is called would lose the lock reference (the holder still
    owns its existing lock object), but Aurora's tests always clear
    between cases without overlap so this is safe.
    """

    _token_cache.clear()
    _token_cache_minted_at.clear()
    _per_installation_locks.clear()
