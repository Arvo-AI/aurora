"""GitHub authentication router: App installation tokens with OAuth fallback.

The auth router is the single entry point for any Aurora subsystem that
needs to call the GitHub REST/GraphQL API on behalf of a user.

Routing rules
-------------
For each call to :func:`get_auth_for_user_repo`:

1. Look up the ``connected_repos`` row for ``(user_id,
   repo_full_name)``, joining ``user_github_installations`` and
   ``github_installations`` so we know in one round-trip whether the
   user still links a non-suspended installation for that repo.
2. If yes: mint an installation token and return
   ``AuthResult(method="app", ...)``.
3. If no AND ``GITHUB_AUTH_MODE`` allows OAuth: look up the user's
   stored OAuth token and return ``AuthResult(method="oauth", ...)``.
4. If still no auth available: raise :class:`NoGitHubAuthError`.

App auth always wins when both are configured, since the installation
token has narrower scope, isolated rate limits, and survives the OAuth
user's GitHub-account departure.

Header construction
-------------------
:func:`make_auth_header` returns ``{"Authorization": "token <value>"}``.
Per GitHub's REST API conventions installation tokens use the same
``token`` prefix as personal access tokens — only the JWT-based App
endpoints (``/app/installations/...``) use the ``Bearer`` prefix, and
those are private to :func:`utils.auth.github_app_token._mint_token`.

Reference: https://docs.github.com/en/rest/overview/authenticating-to-the-rest-api

Security
--------
- Token values are NEVER logged or included in exception messages here.
- Installation token minting is delegated to ``get_installation_token``,
  which already handles per-installation locking, refresh, and
  redaction of any ``ghs_...`` substring that might leak into errors.
- Caching the routing decision is intentionally out of scope: every
  call performs the (cheap) DB lookup so a freshly suspended
  installation is detected on the next call without explicit cache
  invalidation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from utils.auth.github_app_token import (
    GitHubAppInstallationSuspended,
    get_installation_token,
)
from utils.auth.github_auth_mode import is_oauth_enabled
from utils.auth.stateless_auth import get_credentials_from_db, set_rls_context
from utils.db.connection_pool import db_pool
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)


class NoGitHubAuthError(Exception):
    """Raised when no GitHub App credential is available for the (user, repo).

    Cases:

    - No App installation is linked for the repo.
    - The linked installation is suspended on GitHub's side.
    - The user revoked the link (DELETE on ``user_github_installations``).

    Callers (route handlers, agent tools) should map this to a 401/403
    so the frontend can prompt the user to install the GitHub App.
    """


@dataclass(frozen=True)
class AuthResult:
    """Resolved GitHub auth for a (user, repo) pair.

    Attributes:
        method: ``"app"`` for installation tokens, ``"oauth"`` for stored
            user OAuth tokens. Callers emit this as a metric/log dimension.
        token: The credential to place in the Authorization header. Treat
            as a secret: never log, never include in exception messages.
        installation_id: Numeric GitHub installation id when ``method``
            is ``"app"``; ``None`` for OAuth tokens. Useful for
            downstream metrics and webhook correlation.
    """

    method: Literal["app", "oauth"]
    token: str
    installation_id: int | None


def _lookup_repo_installation(
    user_id: str, repo_full_name: str
) -> tuple[int | None, bool]:
    """Return ``(installation_id, has_active_installation)`` for the repo.

    ``has_active_installation`` requires:
        1. the user still links the installation (join row exists),
        2. the installation row exists, and
        3. the installation is not suspended (``suspended_at IS NULL``).

    A LEFT JOIN means a missing user link → ``has_active=False``, which
    sends the caller down the ``NoGitHubAuthError`` path. Returns
    ``(None, False)`` when no ``connected_repos`` row exists for
    this user/repo at all.
    """

    sql = """
        SELECT
            r.installation_id,
            (
                u.installation_id IS NOT NULL
                AND u.disconnected_at IS NULL
                AND i.installation_id IS NOT NULL
                AND i.suspended_at IS NULL
            ) AS has_active_installation
        FROM connected_repos r
        LEFT JOIN user_github_installations u
            ON u.installation_id = r.installation_id
            AND u.user_id = r.user_id
        LEFT JOIN github_installations i
            ON i.installation_id = r.installation_id
        WHERE r.user_id = %s AND r.repo_full_name = %s
        LIMIT 1
    """

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            # ``connected_repos`` (the leading table in this join)
            # is RLS-protected; the auth router can be called from Celery
            # tasks where the connection pool's request-context RLS vars
            # never fired. Resolving + setting org_id explicitly keeps
            # the SELECT readable in every caller context.
            if not set_rls_context(
                cursor, conn, user_id, log_prefix="[GITHUB-AUTH-ROUTER]"
            ):
                return (None, False)
            cursor.execute(sql, (user_id, repo_full_name))
            row = cursor.fetchone()

    if row is None:
        return (None, False)
    installation_id, has_active = row
    return (installation_id, bool(has_active))


def _try_oauth_fallback(user_id: str) -> AuthResult | None:
    """Return an OAuth ``AuthResult`` if the user has a stored token, else None.

    Returns ``None`` when OAuth is disabled in this deployment OR the
    user has no stored OAuth credential. Never raises — credential
    lookup failures degrade to "no auth available", letting the caller
    surface a single ``NoGitHubAuthError``.
    """
    if not is_oauth_enabled():
        return None
    try:
        creds = get_credentials_from_db(user_id, "github")
    except Exception:
        logger.warning(
            "[GITHUB-AUTH-ROUTER] OAuth credential lookup failed for user=%s",
            sanitize(user_id).replace("\r", "_").replace("\n", "_"),
            exc_info=True,
        )
        return None
    token = (creds or {}).get("access_token")
    if not token:
        return None
    return AuthResult(method="oauth", token=token, installation_id=None)


def get_auth_for_user_repo(user_id: str, repo_full_name: str) -> AuthResult:
    """Resolve GitHub auth for a ``(user, repo)`` pair.

    Tries App installation first. When no installation is linked for the
    repo and ``GITHUB_AUTH_MODE`` allows OAuth, falls back to the user's
    stored OAuth token.

    Args:
        user_id: Aurora user id.
        repo_full_name: GitHub ``owner/repo`` slug.

    Returns:
        :class:`AuthResult` with the resolved credential.

    Raises:
        NoGitHubAuthError: No App installation AND no OAuth token (or
            OAuth disabled).
        utils.auth.github_app_token.GitHubAppTokenError: Minting the
            installation token failed for a non-suspension reason
            (network error, installation deleted on GitHub mid-call).
            Callers should map to 401/403 and surface so the user can
            re-install the App.
    """

    installation_id, has_active = _lookup_repo_installation(
        user_id, repo_full_name
    )

    if installation_id is not None and has_active:
        try:
            token = get_installation_token(installation_id)
            return AuthResult(
                method="app",
                token=token,
                installation_id=installation_id,
            )
        except GitHubAppInstallationSuspended:
            # ``repo_full_name`` arrives via a ``<path:...>`` URL converter
            # in github_user_repos.py and could carry CR/LF for log-line
            # forging. Run it through ``sanitize()`` + the literal
            # ``.replace`` chain that Sonar's S5145 rule recognises as
            # neutralisation. ``user_id`` comes from the auth context, but
            # we sanitise it too for symmetry — costs nothing.
            safe_repo = sanitize(repo_full_name).replace("\r", "_").replace("\n", "_")
            safe_user = sanitize(user_id).replace("\r", "_").replace("\n", "_")
            logger.info(
                "[GITHUB-AUTH-ROUTER] App installation suspended at mint time "
                "for installation_id=%d (user=%s repo=%s); attempting OAuth fallback",
                installation_id, safe_user, safe_repo,
            )
            # Fall through to OAuth fallback below.

    oauth_result = _try_oauth_fallback(user_id)
    if oauth_result is not None:
        return oauth_result

    repo_owner = repo_full_name.split("/", 1)[0] if "/" in repo_full_name else None
    if repo_owner:
        fallback_install_id = _lookup_install_by_account(user_id, repo_owner)
        if fallback_install_id is not None:
            try:
                token = get_installation_token(fallback_install_id)
                _backfill_repo_installation(user_id, repo_full_name, fallback_install_id)
                return AuthResult(
                    method="app",
                    token=token,
                    installation_id=fallback_install_id,
                )
            except GitHubAppInstallationSuspended:
                pass

    raise NoGitHubAuthError(
        f"No GitHub credential available for user={user_id} "
        f"repo={repo_full_name}"
    )


def _backfill_repo_installation(
    user_id: str, repo_full_name: str, installation_id: int
) -> None:
    """Best-effort write of ``installation_id`` onto a repo row after fallback resolution."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                if not set_rls_context(
                    cur, conn, user_id, log_prefix="[GITHUB-AUTH-ROUTER]"
                ):
                    return
                cur.execute(
                    """UPDATE connected_repos
                          SET installation_id = %s,
                              updated_at = NOW()
                        WHERE user_id = %s
                          AND repo_full_name = %s
                          AND installation_id IS NULL""",
                    (installation_id, user_id, repo_full_name),
                )
                conn.commit()
    except Exception:
        logger.warning(
            "[GITHUB-AUTH-ROUTER] backfill of installation_id failed for user=%s",
            sanitize(user_id).replace("\r", "_").replace("\n", "_"),
        )


def make_auth_header(auth: AuthResult) -> dict[str, str]:
    """Return the ``Authorization`` header for the given resolved auth.

    Installation tokens use the ``token <value>`` prefix per GitHub
    REST API conventions:
    https://docs.github.com/en/rest/overview/authenticating-to-the-rest-api

    The ``Bearer`` prefix is reserved for the JWT-based App endpoints
    (``/app/installations/...``), an internal concern of
    :func:`utils.auth.github_app_token._mint_token` that never reaches
    this router.
    """
    return {"Authorization": f"token {auth.token}"}


def _lookup_install_by_account(user_id: str, account_login: str) -> int | None:
    """Return the user's active install whose ``account_login`` matches (case-insensitive)."""

    sql = """
        SELECT u.installation_id
        FROM user_github_installations u
        JOIN github_installations i
            ON i.installation_id = u.installation_id
        WHERE u.user_id = %s
          AND u.disconnected_at IS NULL
          AND i.suspended_at IS NULL
          AND LOWER(i.account_login) = LOWER(%s)
        ORDER BY u.is_primary DESC, u.linked_at DESC
        LIMIT 1
    """

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (user_id, account_login))
            row = cursor.fetchone()
    return row[0] if row else None


def _lookup_any_active_installation(user_id: str) -> int | None:
    """Return the first non-suspended installation_id linked to ``user_id``.

    Joins ``user_github_installations`` to ``github_installations`` and
    filters out rows whose installation is suspended or whose
    installation row was deleted (LEFT-JOIN miss). Ordering is
    deterministic — primary installations win, then most recent links —
    so concurrent calls for the same user receive the same installation
    id even when there are multiple candidates.

    Returns ``None`` when the user has no usable installation.
    """

    sql = """
        SELECT u.installation_id
        FROM user_github_installations u
        JOIN github_installations i
            ON i.installation_id = u.installation_id
        WHERE u.user_id = %s
          AND u.disconnected_at IS NULL
          AND i.suspended_at IS NULL
        ORDER BY u.is_primary DESC, u.linked_at DESC
        LIMIT 1
    """

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, (user_id,))
            row = cursor.fetchone()

    if row is None:
        return None
    return row[0]


def get_any_auth_for_user(user_id: str) -> AuthResult:
    """Resolve GitHub App auth for a user WITHOUT a specific repo context.

    Use this when the caller needs a working GitHub credential but does
    not yet know which repository it will operate on (e.g., agent tools
    that list connected repos before deciding which one to investigate).

    Differs from :func:`get_auth_for_user_repo`:

    - ``get_auth_for_user_repo`` looks up the App installation tied to
      ONE specific ``(user_id, repo_full_name)`` pair via
      ``connected_repos.installation_id``.
    - ``get_any_auth_for_user`` looks up the FIRST non-suspended App
      installation linked to the user via ``user_github_installations``,
      regardless of repo.

    Args:
        user_id: Aurora user id.

    Returns:
        :class:`AuthResult` with the installation token.

    Raises:
        NoGitHubAuthError: No usable App installation linked.
        utils.auth.github_app_token.GitHubAppTokenError: Minting failed
            for a non-suspension reason. Callers should surface so the
            user can re-install the App.
    """

    installation_id = _lookup_any_active_installation(user_id)
    if installation_id is not None:
        try:
            token = get_installation_token(installation_id)
            return AuthResult(
                method="app",
                token=token,
                installation_id=installation_id,
            )
        except GitHubAppInstallationSuspended:
            safe_user = sanitize(user_id).replace("\r", "_").replace("\n", "_")
            logger.info(
                "[GITHUB-AUTH-ROUTER] App installation suspended at mint time "
                "for installation_id=%d (user=%s, no repo context); "
                "attempting OAuth fallback",
                installation_id, safe_user,
            )
            # Fall through to OAuth fallback below.

    oauth_result = _try_oauth_fallback(user_id)
    if oauth_result is not None:
        return oauth_result

    raise NoGitHubAuthError(
        f"No GitHub credential available for user={user_id}"
    )


def is_github_connected(user_id: str) -> bool:
    """True if the user has *any* working GitHub credential.

    Used by the skill registry's connection check (and any other code
    that just needs a yes/no). Returns True when EITHER:
      * a non-disconnected, non-suspended App installation is linked, OR
      * an OAuth token is stored (only when OAuth is enabled).

    Never raises — connection checks shouldn't bring down agent
    initialization.
    """
    try:
        if _lookup_any_active_installation(user_id) is not None:
            return True
    except Exception:
        logger.warning(
            "[GITHUB-AUTH-ROUTER] is_github_connected: install lookup failed "
            "for user=%s",
            sanitize(user_id).replace("\r", "_").replace("\n", "_"),
            exc_info=True,
        )

    if is_oauth_enabled():
        try:
            creds = get_credentials_from_db(user_id, "github")
            if creds and creds.get("access_token"):
                return True
        except Exception:
            logger.warning(
                "[GITHUB-AUTH-ROUTER] is_github_connected: oauth lookup failed "
                "for user=%s",
                sanitize(user_id).replace("\r", "_").replace("\n", "_"),
                exc_info=True,
            )

    return False
