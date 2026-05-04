"""GitHub user-repository endpoints (App-preferred, OAuth fallback).

Two read endpoints, both behind ``connectors:read`` RBAC:

    GET /github/user-repos                       -> ``{repos: [...]}``
    GET /github/user-branches/<owner>/<repo>     -> ``{branches: [...]}``

Both routes accept either GitHub App or legacy OAuth credentials per the
auth-router design (see :mod:`utils.auth.github_auth_router`):

* ``/user-repos`` enumerates the user's GitHub App installations from
  ``user_github_installations`` (one batched SELECT, NEVER per-repo) and
  asks each installation's ``GET /installation/repositories`` endpoint for
  the repos it can see. If the user has an OAuth credential, ``GET
  /user/repos`` is also called and the two lists are merged + deduped by
  ``repo_full_name`` with App entries winning on collision (per spec —
  App tokens have finer-grained permissions and higher rate limits).
* ``/user-branches/<repo>`` delegates auth selection to
  :func:`utils.auth.github_auth_router.get_auth_for_user_repo`, which
  returns the App token when the repo was added via the App install flow
  and the OAuth token otherwise.

Each repo in the ``/user-repos`` response carries a new ``auth_method``
field (``"app"`` or ``"oauth"``) plus, for App entries, the
``installation_id``. All other response fields are unchanged.
"""
import logging
import os
import time
from typing import Any
import requests
from flask import Blueprint, jsonify, request
from utils.auth.github_app_token import (
    GitHubAppInstallationSuspended,
    GitHubAppTokenError,
    get_installation_token,
)
from utils.auth.github_auth_router import (
    NoGitHubAuthError,
    get_auth_for_user_repo,
    make_auth_header,
)
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_credentials_from_db
from utils.db.connection_pool import db_pool
from utils.log_sanitizer import sanitize

github_user_repos_bp = Blueprint('github_user_repos', __name__)
logger = logging.getLogger(__name__)

# Match the project-wide GitHub HTTP timeout used elsewhere
# (see ``server/routes/github/github.py``).
_GITHUB_TIMEOUT = 20

# Pagination safety: 50 pages * 100 per_page = 5000 repos / 100 pages * 100 = 10000 branches.
_REPOS_PAGE_LIMIT = 50
_BRANCHES_PAGE_LIMIT = 100
_PER_PAGE = 100


def create_cors_response(data=None, status=200):
    """Create a response with CORS headers"""
    response = jsonify(data) if data else jsonify({})
    response.status_code = status
    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")
    origin = request.headers.get("Origin", frontend_url)
    allowed_origins = {frontend_url, "http://localhost:3000"}
    if origin in allowed_origins:
        response.headers['Access-Control-Allow-Origin'] = origin
    else:
        response.headers['Access-Control-Allow-Origin'] = frontend_url
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-User-ID, X-Org-ID, Authorization'
    response.headers['Access-Control-Allow-Credentials'] = 'true'
    return response


def _list_user_installation_ids(user_id: str) -> list[int]:
    """Return ALL installation_ids linked to ``user_id`` in one DB round-trip.

    Batched so that ``/github/user-repos`` makes exactly ONE query
    against ``user_github_installations`` regardless of how many App
    installations the user has — no N+1.
    """
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT installation_id
                     FROM user_github_installations
                    WHERE user_id = %s
                    ORDER BY installation_id""",
                (user_id,),
            )
            return [row[0] for row in cur.fetchall()]


def _simplify_repo(
    repo: dict[str, Any],
    auth_method: str,
    installation_id: int | None,
) -> dict[str, Any]:
    """Project a GitHub repo payload to Aurora's response shape.

    Adds the new ``auth_method`` and ``installation_id`` fields per spec;
    every other field is forwarded unchanged from the GitHub response so
    existing frontend consumers see no schema regression.
    """
    owner = repo.get("owner") or {}
    return {
        "id": repo.get("id"),
        "name": repo.get("name"),
        "full_name": repo.get("full_name"),
        "private": repo.get("private", False),
        "html_url": repo.get("html_url", ""),
        "description": repo.get("description"),
        "default_branch": repo.get("default_branch", "main"),
        "updated_at": repo.get("updated_at", ""),
        "permissions": repo.get("permissions", {}),
        "owner": {
            "login": owner.get("login", ""),
            "avatar_url": owner.get("avatar_url", ""),
        },
        "auth_method": auth_method,
        "installation_id": installation_id,
    }


def _fetch_installation_repos(token: str, installation_id: int) -> list[dict[str, Any]]:
    """Paginate ``GET /installation/repositories`` for a single App install.

    GitHub returns ``{"total_count": int, "repositories": [...]}`` for
    this endpoint (NOT a bare array — that's only for ``/user/repos``).
    Network/HTTP failures here log + return whatever was already fetched
    so a single broken installation never sinks the whole listing.
    """
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    all_repos: list[dict[str, Any]] = []
    page = 1
    while True:
        try:
            resp = requests.get(
                "https://api.github.com/installation/repositories",
                headers=headers,
                params={"per_page": _PER_PAGE, "page": page},
                timeout=_GITHUB_TIMEOUT,
            )
        except requests.RequestException as exc:
            logger.warning(
                "[USER-REPOS] App-mode list failed installation_id=%d: %s",
                installation_id, type(exc).__name__,
            )
            break
        if resp.status_code != 200:
            logger.error(
                "[USER-REPOS] App-mode list returned status=%d for installation_id=%d",
                resp.status_code, installation_id,
            )
            break
        try:
            payload = resp.json()
        except ValueError:
            logger.error(
                "[USER-REPOS] App-mode response not JSON for installation_id=%d",
                installation_id,
            )
            break
        repos = payload.get("repositories", []) if isinstance(payload, dict) else []
        if not repos:
            break
        all_repos.extend(repos)
        if len(repos) < _PER_PAGE:
            break
        page += 1
        if page > _REPOS_PAGE_LIMIT:
            logger.warning(
                "[USER-REPOS] App-mode pagination safety limit hit for installation_id=%d",
                installation_id,
            )
            break
    return all_repos


def _fetch_oauth_repos(token: str) -> list[dict[str, Any]]:
    """Paginate ``GET /user/repos`` using a legacy OAuth token."""
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    all_repos: list[dict[str, Any]] = []
    page = 1
    while True:
        try:
            resp = requests.get(
                "https://api.github.com/user/repos",
                headers=headers,
                params={"sort": "updated", "per_page": _PER_PAGE, "page": page},
                timeout=_GITHUB_TIMEOUT,
            )
        except requests.RequestException as exc:
            logger.warning(
                "[USER-REPOS] OAuth-mode list failed: %s", type(exc).__name__,
            )
            break
        if resp.status_code != 200:
            logger.error(
                "[USER-REPOS] OAuth-mode list returned status=%d", resp.status_code,
            )
            break
        try:
            repos = resp.json()
        except ValueError:
            logger.error("[USER-REPOS] OAuth-mode response not JSON")
            break
        if not isinstance(repos, list) or not repos:
            break
        all_repos.extend(repos)
        if len(repos) < _PER_PAGE:
            break
        page += 1
        if page > _REPOS_PAGE_LIMIT:
            logger.warning("[USER-REPOS] OAuth-mode pagination safety limit hit")
            break
    return all_repos


def _list_repos_for_user(user_id: str) -> list[dict[str, Any]]:
    """Return merged App+OAuth repos for ``user_id`` with auth tagging.

    Order of operations (matches the auth-router precedence):

    1. Batched lookup of ``user_github_installations`` (single SELECT).
    2. For each installation, mint an installation token and call
       ``GET /installation/repositories``. Suspended installations are
       skipped silently (logged at INFO) so a partially-broken account
       still gets the rest of its repos.
    3. If the user has an OAuth credential, call ``GET /user/repos`` and
       only include OAuth repos with ``permissions.push == True`` (the
       legacy filter — App-installed repos are pre-filtered by GitHub
       so they don't need this check).
    4. Dedupe by ``repo_full_name``: App entries written first, OAuth
       entries skipped when their ``full_name`` already exists. App wins.
    """
    repos_by_full_name: dict[str, dict[str, Any]] = {}

    installation_ids = _list_user_installation_ids(user_id)
    for installation_id in installation_ids:
        try:
            token = get_installation_token(installation_id)
        except GitHubAppInstallationSuspended:
            logger.info(
                "[USER-REPOS] Skipping suspended installation_id=%d for user=%s",
                installation_id, user_id,
            )
            continue
        except GitHubAppTokenError as exc:
            logger.warning(
                "[USER-REPOS] Token mint failed for installation_id=%d user=%s: %s",
                installation_id, user_id, type(exc).__name__,
            )
            continue

        for repo in _fetch_installation_repos(token, installation_id):
            full_name = repo.get("full_name")
            if not full_name:
                continue
            # First write wins per (full_name) at the App layer; multiple
            # installations of the same App on org-A and org-B legitimately
            # exposing the same repo is rare but tolerated here.
            if full_name not in repos_by_full_name:
                repos_by_full_name[full_name] = _simplify_repo(
                    repo, "app", installation_id,
                )

    github_creds = get_credentials_from_db(user_id, "github")
    oauth_token = github_creds.get("access_token") if github_creds else None
    if oauth_token:
        for repo in _fetch_oauth_repos(oauth_token):
            full_name = repo.get("full_name")
            if not full_name or full_name in repos_by_full_name:
                continue
            if not repo.get("permissions", {}).get("push", False):
                continue
            repos_by_full_name[full_name] = _simplify_repo(repo, "oauth", None)

    return list(repos_by_full_name.values())


@github_user_repos_bp.route("/user-repos", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def get_user_repos(user_id):
    """List all GitHub repos accessible to ``user_id`` (App + OAuth, deduped)."""
    if request.method == 'OPTIONS':
        return create_cors_response()

    t0 = time.time()
    try:
        repos = _list_repos_for_user(user_id)
        logger.info(
            "Fetched %d repositories for user in %dms",
            len(repos), int((time.time() - t0) * 1000),
        )
        return create_cors_response({"repos": repos})
    except Exception as e:
        logger.error(f"Error fetching user repositories: {e}", exc_info=True)
        return create_cors_response(
            {"error": "Failed to fetch repositories", "repos": []}, 500,
        )


@github_user_repos_bp.route("/user-branches/<path:repo_full_name>", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def get_user_branches(user_id, repo_full_name):
    """List branches for ``repo_full_name`` using App-preferred auth."""
    if request.method == 'OPTIONS':
        return create_cors_response()

    # Pre-sanitize the user-controlled path component before any log
    # statement. ``sanitize()`` strips C0/C1 + Unicode line separators
    # (the project's S5145 helper); the chained ``replace`` calls fold
    # any residual CR/LF that snuck in via tools that didn't go through
    # the helper, which is the literal pattern Sonar's S5145 rule
    # recognizes as sanitization.
    safe_repo = sanitize(repo_full_name).replace("\r", "_").replace("\n", "_")

    try:
        try:
            auth = get_auth_for_user_repo(user_id, repo_full_name)
        except NoGitHubAuthError as exc:
            logger.warning(
                "[USER-BRANCHES] No GitHub auth user=%s repo=%s: %s",
                user_id, safe_repo, exc,
            )
            return create_cors_response(
                {
                    "error": "No GitHub credentials available for this repository",
                    "branches": [],
                },
                401,
            )

        headers = {
            **make_auth_header(auth),
            "Accept": "application/vnd.github.v3+json",
        }

        all_branches = []
        page = 1
        while True:
            response = requests.get(
                f"https://api.github.com/repos/{repo_full_name}/branches",
                headers=headers,
                params={"per_page": _PER_PAGE, "page": page},
                timeout=_GITHUB_TIMEOUT,
            )

            if response.status_code != 200:
                logger.error(f"GitHub API error: {response.status_code}")
                return create_cors_response(
                    {"error": "Failed to fetch branches", "branches": []}, 200,
                )

            branches = response.json()

            if not branches:
                break

            all_branches.extend(branches)

            if len(branches) < _PER_PAGE:
                break

            page += 1

            if page > _BRANCHES_PAGE_LIMIT:
                logger.warning("Hit pagination safety limit for repo %s", safe_repo)
                break

        logger.info(
            "Fetched %d branches for repo %s via %s",
            len(all_branches), safe_repo, auth.method,
        )
        return create_cors_response({"branches": all_branches})

    except Exception as e:
        logger.error(f"Error fetching branches: {e}", exc_info=True)
        return create_cors_response(
            {"error": "Failed to fetch branches", "branches": []}, 500,
        )
