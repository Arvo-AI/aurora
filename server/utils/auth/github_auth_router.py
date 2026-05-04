"""GitHub authentication router: prefer App tokens, fall back to OAuth.

The auth router is the single entry point for any Aurora subsystem that
needs to call the GitHub REST/GraphQL API on behalf of a user. It
encapsulates the dual-mode auth contract: every Aurora user may have
either or both of the following credentials available:

- An OAuth credential (legacy path), stored under
  ``user_tokens.provider='github'`` and resolved through Vault by
  :func:`utils.auth.stateless_auth.get_credentials_from_db`.
- One or more GitHub App installations (new path), joined through
  ``github_connected_repos`` to a non-suspended row in
  ``github_installations``, with installation tokens minted on demand by
  :func:`utils.auth.github_app_token.get_installation_token`.

Routing rules
-------------
For each call to :func:`get_auth_for_user_repo` we:

1. Look up the ``github_connected_repos`` row for ``(user_id,
   repo_full_name)``, ``LEFT JOIN``-ing ``github_installations`` so we
   get ``installation_id`` and ``suspended_at`` in a single round-trip.
2. If ``installation_id`` is non-``NULL`` AND the joined installation
   row is present AND ``suspended_at IS NULL``: mint an installation
   token and return ``AuthResult(method="app", ...)``. App mode is
   preferred because it has finer-grained permissions, higher rate
   limits, and is account-independent.
3. On :class:`utils.auth.github_app_token.GitHubAppInstallationSuspended`
   raised at mint time (race between DB snapshot and the GitHub API
   call): log the suspension at INFO and try the OAuth path. If OAuth
   is available, return ``AuthResult(method="oauth", ...)``. If neither
   credential is available, raise :class:`NoGitHubAuthError`.
4. If App auth is not available (no ``installation_id``, suspended row,
   or installation row missing): try OAuth. Return
   ``AuthResult(method="oauth", ...)`` if a credential is found.
5. If neither is available: raise :class:`NoGitHubAuthError`.

Header construction
-------------------
:func:`make_auth_header` returns ``{"Authorization": "token <value>"}``
for BOTH methods. Per GitHub's REST API conventions, installation
tokens use the same ``token`` prefix as personal access tokens — only
the JWT-based App endpoints (``/app/installations/...``) use the
``Bearer`` prefix, and those are private to
:func:`utils.auth.github_app_token._mint_token`.

Reference: https://docs.github.com/en/rest/overview/authenticating-to-the-rest-api

Security
--------
- Token values are NEVER logged or included in exception messages here.
- OAuth retrieval is delegated unmodified to ``get_credentials_from_db``
  (Vault-backed, dual-session aware).
- Installation token minting is delegated to ``get_installation_token``,
  which already handles per-installation locking, refresh, and
  redaction of any ``ghs_...`` substring that might leak into errors.
- Caching the routing decision is intentionally out of scope: every
  call performs the (cheap) DB lookup so a freshly suspended
  installation is detected on the next call without explicit cache
  invalidation. A future Redis-backed memoization layer is documented
  in the migration plan as out-of-scope for this task.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from utils.auth.github_app_token import (
    GitHubAppInstallationSuspended,
    get_installation_token,
)
from utils.auth.stateless_auth import get_credentials_from_db
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)


class NoGitHubAuthError(Exception):
    """Raised when no GitHub credential is available for the (user, repo).

    Either:

    - No App installation is linked AND no OAuth credential exists, or
    - The only available App installation is suspended AND no OAuth
      credential exists for fallback.

    Callers (route handlers, agent tools) should map this to a 401/403
    so the frontend can prompt for re-auth (Install App or Connect OAuth).
    """


@dataclass(frozen=True)
class AuthResult:
    """Resolved GitHub auth for a (user, repo) pair.

    Attributes:
        method: Which auth path produced the token. ``"app"`` means a
            GitHub App installation token (preferred). ``"oauth"`` means
            the legacy user OAuth token.
        token: The credential string to place in the Authorization
            header. Treat as a secret: never log, never include in
            exception messages.
        installation_id: Numeric GitHub installation id when
            ``method == "app"``, otherwise ``None``. Useful for
            downstream metrics and webhook correlation.
    """

    method: Literal["app", "oauth"]
    token: str
    installation_id: int | None


def _lookup_repo_installation(
    user_id: str, repo_full_name: str
) -> tuple[int | None, bool | None]:
    """Return ``(installation_id, has_active_installation)`` for the repo.

    Performs a single ``LEFT JOIN`` against ``github_installations`` so
    we know in one round-trip both:

    - Whether ``github_connected_repos.installation_id`` is non-``NULL``
      (i.e., the repo was added via the App install flow).
    - Whether the joined ``github_installations`` row exists AND has
      ``suspended_at IS NULL`` (i.e., App auth is currently usable).

    The ``has_active_installation`` flag is:

    - ``None`` when no ``github_connected_repos`` row exists (caller
      will fall through to OAuth).
    - ``False`` when the repo row exists but its installation_id is
      ``NULL``, the joined installation row is missing, or it is
      suspended.
    - ``True`` when the App path is usable.

    The query uses :func:`db_pool.get_admin_connection` (RLS-bypassing)
    because ``WHERE user_id = %s`` already scopes to the calling user
    and the router is invoked from contexts (Celery workers, install
    callbacks) that may not have the Flask request session needed to
    set ``myapp.current_user_id`` for RLS.
    """

    # Also join ``user_github_installations`` so an unlink (DELETE on that
    # join row) immediately revokes App-token minting even when the
    # ``github_connected_repos.installation_id`` was set during a prior
    # repo-selection write. ``has_active`` requires:
    #   1. the user still links the installation (u.installation_id present),
    #   2. the installation row exists (i.installation_id present), and
    #   3. the installation is not suspended (suspended_at IS NULL).
    # A LEFT JOIN means missing user link → has_active=False, which sends
    # the caller down the OAuth fallback path.
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
            cursor.execute(sql, (user_id, repo_full_name))
            row = cursor.fetchone()

    if row is None:
        return (None, None)
    installation_id, has_active = row
    return (installation_id, has_active)


def _try_oauth(user_id: str) -> str | None:
    creds = get_credentials_from_db(user_id, "github")
    if creds and creds.get("access_token"):
        return creds["access_token"]
    return None


def get_auth_for_user_repo(user_id: str, repo_full_name: str) -> AuthResult:
    """Resolve GitHub auth for a ``(user, repo)`` pair, App preferred.

    Args:
        user_id: Aurora user id (matches ``users.id``,
            ``github_connected_repos.user_id``, etc.).
        repo_full_name: GitHub ``owner/repo`` slug.

    Returns:
        :class:`AuthResult` populated with whichever credential is
        available, preferring the App path when both are present and
        the installation is not suspended.

    Raises:
        NoGitHubAuthError: Neither App nor OAuth auth is available
            (or App is suspended AND there is no OAuth fallback).
        utils.auth.github_app_token.GitHubAppTokenError: App auth was
            selected but minting the installation token failed for a
            reason other than suspension (e.g., network error,
            installation deleted on GitHub mid-call). Callers should
            map to 401/403 and surface the error to the user so they
            can re-install the App.
    """

    installation_id, has_active = _lookup_repo_installation(
        user_id, repo_full_name
    )

    # App-preferred path: only attempt if we have an installation_id AND
    # the joined installation row is present and not suspended. The
    # truthiness check on ``has_active`` covers True / False / None
    # (None means "row missing", which we treat as not-active).
    if installation_id is not None and has_active:
        try:
            token = get_installation_token(installation_id)
            return AuthResult(
                method="app",
                token=token,
                installation_id=installation_id,
            )
        except GitHubAppInstallationSuspended:
            # The installation may have been suspended between our DB
            # snapshot and the GitHub API call (race), or the local
            # ``github_installations.suspended_at`` is stale. Fall back
            # to OAuth if the user has one; otherwise re-raise as
            # ``NoGitHubAuthError`` so callers don't need to know about
            # App-specific exception types.
            logger.info(
                "[GITHUB-AUTH-ROUTER] App installation suspended at mint time "
                "for installation_id=%d (user=%s repo=%s); attempting OAuth fallback",
                installation_id,
                user_id,
                repo_full_name,
            )
            oauth_token = _try_oauth(user_id)
            if oauth_token is not None:
                return AuthResult(
                    method="oauth",
                    token=oauth_token,
                    installation_id=None,
                )
            raise NoGitHubAuthError(
                f"GitHub App installation_id={installation_id} is suspended "
                f"and no OAuth credential is available for user={user_id}"
            )

    oauth_token = _try_oauth(user_id)
    if oauth_token is not None:
        return AuthResult(
            method="oauth",
            token=oauth_token,
            installation_id=None,
        )

    raise NoGitHubAuthError(
        f"No GitHub auth available for user={user_id} repo={repo_full_name} "
        "(no App installation and no OAuth credential)"
    )


def make_auth_header(auth: AuthResult) -> dict[str, str]:
    """Return the ``Authorization`` header for the given resolved auth.

    Both App installation tokens and OAuth user access tokens use the
    ``token <value>`` prefix per GitHub REST API conventions:
    https://docs.github.com/en/rest/overview/authenticating-to-the-rest-api

    The ``Bearer`` prefix is reserved for the JWT-based App endpoints
    (``/app/installations/...``), which are an internal concern of
    :func:`utils.auth.github_app_token._mint_token` and never reach
    this router.
    """

    return {"Authorization": f"token {auth.token}"}


def _lookup_any_active_installation(user_id: str) -> int | None:
    """Return the first non-suspended installation_id linked to ``user_id``.

    Joins ``user_github_installations`` to ``github_installations`` and
    filters out rows whose installation is suspended (``suspended_at IS
    NOT NULL``) or whose installation row was deleted (LEFT-JOIN miss).
    Ordering is deterministic — primary installations win, then most
    recent links — so concurrent calls for the same user receive the
    same installation id even when there are multiple candidates.

    Returns ``None`` when the user has no linked installation, or when
    every linked installation is suspended/orphaned. Callers should fall
    through to OAuth in that case.
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
    """Resolve GitHub auth for a user WITHOUT a specific repo context.

    Use this when the caller needs a working GitHub credential but does
    not yet know which repository it will operate on (e.g., agent tools
    that list connected repos before deciding which one to investigate).

    Differs from :func:`get_auth_for_user_repo`:

    - ``get_auth_for_user_repo`` looks up the App installation tied to
      ONE specific ``(user_id, repo_full_name)`` pair via
      ``github_connected_repos.installation_id``. App auth is preferred
      iff that specific repo is reachable through a non-suspended
      installation.
    - ``get_any_auth_for_user`` looks up the FIRST non-suspended App
      installation linked to the user via ``user_github_installations``
      regardless of repo. App auth is preferred whenever any such
      installation exists.

    The two-helpers split is intentional: per-repo routing must respect
    the repo's actual installation linkage (a repo connected through
    OAuth must not opportunistically use a different App installation
    on the same account), whereas user-level operations can pick any
    available installation token.

    Routing rules
    -------------
    1. If the user has at least one linked, non-suspended App
       installation: mint that installation's token and return
       ``AuthResult(method="app", ...)``.
    2. On :class:`utils.auth.github_app_token.GitHubAppInstallationSuspended`
       at mint time (race between DB snapshot and GitHub API call): fall
       back to OAuth if available; otherwise raise
       :class:`NoGitHubAuthError`.
    3. If no usable App installation exists: try OAuth and return
       ``AuthResult(method="oauth", ...)`` if a credential is found.
    4. If neither is available: raise :class:`NoGitHubAuthError`.

    Args:
        user_id: Aurora user id (matches ``users.id``).

    Returns:
        :class:`AuthResult` populated with whichever credential is
        available, preferring an App installation token when any
        non-suspended installation is linked to the user.

    Raises:
        NoGitHubAuthError: Neither App nor OAuth auth is available
            (no linked non-suspended installation AND no OAuth
            credential).
        utils.auth.github_app_token.GitHubAppTokenError: An App
            installation was selected but minting the token failed for
            a non-suspension reason (network error, deleted on
            GitHub mid-call). Callers should surface this so the user
            can re-install the App.
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
            # The installation may have been suspended between our DB
            # snapshot and the GitHub API call (race), or the local
            # ``github_installations.suspended_at`` is stale. Fall back
            # to OAuth if the user has one; otherwise raise
            # ``NoGitHubAuthError`` so callers don't need to know about
            # App-specific exception types.
            logger.info(
                "[GITHUB-AUTH-ROUTER] App installation suspended at mint time "
                "for installation_id=%d (user=%s, no repo context); "
                "attempting OAuth fallback",
                installation_id,
                user_id,
            )
            oauth_token = _try_oauth(user_id)
            if oauth_token is not None:
                return AuthResult(
                    method="oauth",
                    token=oauth_token,
                    installation_id=None,
                )
            raise NoGitHubAuthError(
                f"GitHub App installation_id={installation_id} is suspended "
                f"and no OAuth credential is available for user={user_id}"
            )

    oauth_token = _try_oauth(user_id)
    if oauth_token is not None:
        return AuthResult(
            method="oauth",
            token=oauth_token,
            installation_id=None,
        )

    raise NoGitHubAuthError(
        f"No GitHub auth available for user={user_id} "
        "(no linked App installation and no OAuth credential)"
    )
