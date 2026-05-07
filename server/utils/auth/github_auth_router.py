"""GitHub authentication router: GitHub App installation tokens only.

The auth router is the single entry point for any Aurora subsystem that
needs to call the GitHub REST/GraphQL API on behalf of a user.

Routing rules
-------------
For each call to :func:`get_auth_for_user_repo`:

1. Look up the ``github_connected_repos`` row for ``(user_id,
   repo_full_name)``, joining ``user_github_installations`` and
   ``github_installations`` so we know in one round-trip whether the
   user still links a non-suspended installation for that repo.
2. If yes: mint an installation token and return
   ``AuthResult(method="app", ...)``.
3. If no (no row, no link, suspended): raise :class:`NoGitHubAuthError`.

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
from utils.auth.stateless_auth import set_rls_context
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
        method: Always ``"app"`` — only auth path supported. Kept as a
            field for callers that emit it as a metric/log dimension.
        token: The installation token to place in the Authorization
            header. Treat as a secret: never log, never include in
            exception messages.
        installation_id: Numeric GitHub installation id. Useful for
            downstream metrics and webhook correlation.
    """

    method: Literal["app"]
    token: str
    installation_id: int


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
    ``(None, False)`` when no ``github_connected_repos`` row exists for
    this user/repo at all.
    """

    sql = """
        SELECT
            r.installation_id,
            (
                u.installation_id IS NOT NULL
                AND i.installation_id IS NOT NULL
                AND i.suspended_at IS NULL
            ) AS has_active_installation
        FROM github_connected_repos r
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
            # ``github_connected_repos`` (the leading table in this join)
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


def get_auth_for_user_repo(user_id: str, repo_full_name: str) -> AuthResult:
    """Resolve GitHub App auth for a ``(user, repo)`` pair.

    Args:
        user_id: Aurora user id.
        repo_full_name: GitHub ``owner/repo`` slug.

    Returns:
        :class:`AuthResult` with the installation token.

    Raises:
        NoGitHubAuthError: No active App installation linked for this
            repo (no row, link revoked, or installation suspended).
        utils.auth.github_app_token.GitHubAppTokenError: Minting the
            installation token failed for a non-suspension reason
            (network error, installation deleted on GitHub mid-call).
            Callers should map to 401/403 and surface so the user can
            re-install the App.
    """

    installation_id, has_active = _lookup_repo_installation(
        user_id, repo_full_name
    )

    if installation_id is None or not has_active:
        raise NoGitHubAuthError(
            f"No GitHub App installation available for user={user_id} "
            f"repo={repo_full_name}"
        )

    try:
        token = get_installation_token(installation_id)
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
            "for installation_id=%d (user=%s repo=%s)",
            installation_id, safe_user, safe_repo,
        )
        raise NoGitHubAuthError(
            f"GitHub App installation_id={installation_id} is suspended"
        )

    return AuthResult(
        method="app",
        token=token,
        installation_id=installation_id,
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
      ``github_connected_repos.installation_id``.
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
    if installation_id is None:
        raise NoGitHubAuthError(
            f"No GitHub App installation available for user={user_id}"
        )

    try:
        token = get_installation_token(installation_id)
    except GitHubAppInstallationSuspended:
        safe_user = sanitize(user_id).replace("\r", "_").replace("\n", "_")
        logger.info(
            "[GITHUB-AUTH-ROUTER] App installation suspended at mint time "
            "for installation_id=%d (user=%s, no repo context)",
            installation_id, safe_user,
        )
        raise NoGitHubAuthError(
            f"GitHub App installation_id={installation_id} is suspended"
        )

    return AuthResult(
        method="app",
        token=token,
        installation_id=installation_id,
    )
