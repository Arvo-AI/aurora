"""GitHub auth-mode resolution for hybrid (App + OAuth) deployments.

Aurora ships GitHub-App-only by default. On-prem operators who cannot host
their own App can switch to OAuth or hybrid mode via the ``GITHUB_AUTH_MODE``
env var. This module is the single source of truth that backend routes,
the auth router, and the ``/github/auth-config`` endpoint all read from.

OAuth onboarding is deprecated in favour of the GitHub App. Two concerns are
kept separate so deprecation does not orphan existing users:
  * NEW OAuth connections (the ``/github/login`` flow + the "Connect via
    OAuth" CTA) — gated by :func:`is_oauth_login_enabled`, OFF in ``app`` mode.
  * EXISTING stored OAuth tokens (status, token resolution, repo listing) —
    gated by :func:`is_oauth_token_honored`, honoured in EVERY mode so a user
    who connected via OAuth before the App migration keeps working until they
    disconnect (then they reconnect via the App).

Modes:
    ``app``     — GitHub App for all NEW connections. ``/github/login`` returns
                  404 and the dialog shows only the Install GitHub App CTA, but
                  EXISTING OAuth tokens are still honoured. This is the default.
    ``oauth``   — OAuth onboarding enabled. App-install routes still respond (so
                  existing installs are not orphaned), but the dialog hides the
                  App CTA. ``GH_OAUTH_CLIENT_ID`` / ``GH_OAUTH_CLIENT_SECRET``
                  must be set or login returns ``GITHUB_NOT_CONFIGURED``.
    ``hybrid``  — Both onboarding paths active. Dialog shows both CTAs. Auth
                  router prefers App installation tokens when available and
                  falls back to user OAuth tokens otherwise.

The resolved mode is exposed to the frontend via the ``/github/auth-config``
endpoint so the client never has to trust ``NEXT_PUBLIC_*`` env vars for
this decision.
"""

from __future__ import annotations

import os
from typing import Literal

GitHubAuthMode = Literal["app", "oauth", "hybrid"]

_VALID_MODES: tuple[GitHubAuthMode, ...] = ("app", "oauth", "hybrid")
_DEFAULT_MODE: GitHubAuthMode = "app"


def get_auth_mode() -> GitHubAuthMode:
    """Read ``GITHUB_AUTH_MODE`` from env, defaulting to ``app``.

    Unrecognized values fall back to ``app`` so a typo cannot silently
    disable the App path that most deployments rely on.
    """
    raw = (os.getenv("GITHUB_AUTH_MODE") or "").strip().lower()
    if raw in _VALID_MODES:
        return raw  # type: ignore[return-value]
    return _DEFAULT_MODE


# Existing OAuth tokens are honoured in every mode: onboarding is deprecated,
# but established connections must keep working. A dedicated tuple documents
# this (rather than a bare ``return True``) and leaves room for a future
# "hard-off" mode that opts out of honouring existing tokens.
_OAUTH_TOKEN_HONORED_MODES: tuple[GitHubAuthMode, ...] = _VALID_MODES


def is_oauth_login_enabled() -> bool:
    """True if the deployment offers NEW OAuth connections.

    Governs the ``/github/login`` flow and the connector "Connect via OAuth"
    CTA. OAuth onboarding is deprecated in favour of the GitHub App, so the
    default ``app`` mode returns False — only explicit ``oauth`` / ``hybrid``
    deployments still expose it. Existing OAuth connections are unaffected
    (see :func:`is_oauth_token_honored`).
    """
    return get_auth_mode() in ("oauth", "hybrid")


def is_oauth_token_honored() -> bool:
    """True if EXISTING stored OAuth tokens are still read and used.

    OAuth onboarding is deprecated, but a user who connected via OAuth before
    the App migration keeps that connection working until they disconnect (then
    they reconnect via the App). So existing tokens are honoured in every mode,
    including the default ``app`` mode where they were previously dropped. In a
    pure-App deployment no OAuth tokens exist, so this is a harmless no-op there.
    """
    return get_auth_mode() in _OAUTH_TOKEN_HONORED_MODES


def is_app_enabled() -> bool:
    """True if the deployment exposes the GitHub App install path."""
    return get_auth_mode() in ("app", "hybrid")


def oauth_credentials_configured() -> bool:
    """True if both ``GH_OAUTH_CLIENT_ID`` and ``GH_OAUTH_CLIENT_SECRET`` are set.

    Used by ``/github/auth-config`` to surface a misconfiguration to the
    frontend up-front, before the user clicks "Connect via OAuth" and gets
    a generic 400 from the login route.
    """
    return bool(os.getenv("GH_OAUTH_CLIENT_ID")) and bool(
        os.getenv("GH_OAUTH_CLIENT_SECRET")
    )
