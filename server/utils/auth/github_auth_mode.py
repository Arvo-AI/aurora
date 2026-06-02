"""GitHub auth-mode resolution for hybrid (App + OAuth) deployments.

Aurora ships GitHub-App-only by default. On-prem operators who cannot host
their own App can switch to OAuth or hybrid mode via the ``GITHUB_AUTH_MODE``
env var. This module is the single source of truth that backend routes,
the auth router, and the ``/github/auth-config`` endpoint all read from.

Modes:
    ``app``     — GitHub App only. ``/github/login`` returns 404. The
                  connector dialog shows only the Install GitHub App CTA.
                  This is the default.
    ``oauth``   — OAuth only. App-install routes still respond (so existing
                  installs are not orphaned), but the dialog hides the App
                  CTA. ``GH_OAUTH_CLIENT_ID`` / ``GH_OAUTH_CLIENT_SECRET``
                  must be set or login returns ``GITHUB_NOT_CONFIGURED``.
    ``hybrid``  — Both paths active. Dialog shows both CTAs. Auth router
                  prefers App installation tokens when available and falls
                  back to user OAuth tokens otherwise.

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


def is_oauth_enabled() -> bool:
    """True if the deployment exposes an OAuth login path."""
    return get_auth_mode() in ("oauth", "hybrid")


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
