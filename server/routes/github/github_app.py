"""GitHub App install + management routes (App-only auth, no user tokens).

Four endpoints, all under the ``/github`` URL prefix registered in
:mod:`main_compute`:

    GET    /github/app/install                   -> JSON ``{install_url}``
    GET    /github/app/install/callback           -> renders success/error template
    GET    /github/app/installations              -> JSON ``{installations: [...]}``
    DELETE /github/app/installations/<int:id>    -> removes user->install join row

Anti-spoofing invariants for the callback (do NOT relax):

    1. ``installation_id`` MUST be verified via
       ``GET /app/installations/{id}`` before any DB write. A 404 from GitHub
       indicates a spoofed/forged callback - render the error template and
       insert ZERO rows.
    2. ``state`` MUST resolve to a known Aurora user via
       :func:`utils.auth.stateless_auth.validate_user_exists`. Unknown ->
       error template + zero rows.
    3. The error template is rendered with HARD-CODED constant strings only.
       Query params are never substituted into the template - this avoids
       both HTML XSS (Jinja autoescape covers HTML context but the inline
       JS in the template is a separate, harder-to-audit context) and any
       reflected-data leak.
    4. The ``DELETE`` endpoint removes ONLY the user->installation join row.
       It does NOT call GitHub to uninstall the App; that is user-driven via
       GitHub's UI per spec.

This module is App-only. It does NOT issue or store user OAuth tokens; the
existing OAuth flow in :mod:`routes.github.github` remains the path for that.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

import flask
import requests
from flask import Blueprint, jsonify, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from utils.auth.github_app_jwt import GitHubAppJWTError, mint_app_jwt
from utils.auth.github_auth_mode import (
    get_auth_mode,
    is_app_enabled,
    is_oauth_login_enabled,
    is_oauth_token_honored,
    oauth_credentials_configured,
)
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import (
    get_credentials_from_db,
    get_org_id_for_user,
    validate_user_exists,
)
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

github_app_bp = Blueprint("github_app", __name__)

FRONTEND_URL = os.getenv("FRONTEND_URL") or ""
GITHUB_TIMEOUT = 20
GITHUB_RECONCILE_TIMEOUT = 3
_GH_JSON_MEDIA_TYPE = "application/vnd.github+json"
_APP_NOT_CONFIGURED = "GitHub App not configured"


def _orgs_owning_install(installation_id: int) -> set[str | None]:
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT DISTINCT org_id
                     FROM user_github_installations
                    WHERE installation_id = %s
                      AND disconnected_at IS NULL""",
                (installation_id,),
            )
            return {row[0] for row in cur.fetchall()}


# Installs this user may relink: they held a link to it in this org, and
# this org held it last. Nothing else proves access to an install no org
# holds now, and linking it imports every repo it covers.
_RELINKABLE_SQL = """
    SELECT mine.installation_id
      FROM user_github_installations mine
     WHERE mine.user_id = %s
       AND mine.org_id = %s
       AND mine.disconnected_at IS NOT NULL
       AND (SELECT latest.org_id
              FROM user_github_installations latest
             WHERE latest.installation_id = mine.installation_id
             ORDER BY latest.disconnected_at DESC NULLS FIRST
             LIMIT 1) = %s"""


def _relinkable_installs(cur, user_id: str, org_id: str) -> set[int]:
    cur.execute(_RELINKABLE_SQL, (user_id, org_id, org_id))
    return {row[0] for row in cur.fetchall()}


def _can_relink(user_id: str, org_id: str, installation_id: int) -> bool:
    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        return installation_id in _relinkable_installs(cur, user_id, org_id)


def hand_off_import_state(cur, user_id: str, installation_ids: list[int] | None = None) -> None:
    """Move a leaving user's import marker to teammates still linked.

    The marker and the legacy flag describe the org's repos, not the user:
    without this, a teammate's unmarked link would trigger a full import
    that brings back repos the org removed.
    """
    cur.execute(
        """UPDATE user_github_installations other
              SET repos_imported_at = COALESCE(other.repos_imported_at, mine.repos_imported_at),
                  repos_import_legacy = other.repos_import_legacy OR mine.repos_import_legacy
             FROM user_github_installations mine
            WHERE mine.user_id = %s
              AND mine.disconnected_at IS NULL
              AND (%s::bigint[] IS NULL OR mine.installation_id = ANY(%s::bigint[]))
              AND other.installation_id = mine.installation_id
              AND other.org_id = mine.org_id
              AND other.user_id <> mine.user_id
              AND other.disconnected_at IS NULL""",
        (user_id, installation_ids, installation_ids),
    )


# Install-state TTL: GitHub's install flow takes seconds in practice, but
# allow 30 minutes to absorb account-creation, 2FA, OAuth-grant, popup-block
# detours. After expiry the user must re-initiate the install.
_INSTALL_STATE_TTL_SEC = 30 * 60
_INSTALL_STATE_SALT = "aurora.github.app.install-state.v1"

# Hard-coded user-facing error strings. NEVER substitute query params.
_ERROR_MISSING_PARAMS = "Missing required parameters from GitHub callback"
_ERROR_BAD_INSTALL_ID = "GitHub installation could not be verified"
_ERROR_UNKNOWN_USER = "User identity could not be verified"
_ERROR_INVALID_STATE = "Install request could not be verified. Please try installing again."
_ERROR_GITHUB_API = "Could not verify installation with GitHub"
_ERROR_INTERNAL = "An internal error occurred while finalizing installation"
_ERROR_NOT_CONFIGURED = "GitHub App is not configured"
_ERROR_INSTALL_NOT_AUTHORIZED = "Your GitHub account does not have access to this installation"
_ERROR_LINKED_ELSEWHERE = "This GitHub installation is already connected to another Aurora organization"


def _installer_can_access(code: str | None, installation_id: int) -> bool:
    """Whether the installer can access ``installation_id`` on GitHub.

    Hosted deployments (client secret set, OAuth during install on) require
    the OAuth code and check it. Without a client secret there is nothing
    to check it with; the cross-org refusal still applies.
    """
    code = (code or "").strip()
    if not os.getenv("GITHUB_APP_CLIENT_SECRET"):
        # Self-hosted without OAuth during install: nothing to verify with.
        return True
    if not code:
        # With OAuth during install on, GitHub always sends a code; a
        # callback without one was not produced by GitHub's install flow.
        logger.warning("[GITHUB-APP-CALLBACK] callback without OAuth code refused")
        return False
    from routes.github.github_signup import (
        _exchange_code_for_identity,
        _user_authorized_for_installation,
    )

    exchanged = _exchange_code_for_identity(code)
    if exchanged is None:
        logger.warning("[GITHUB-APP-CALLBACK] OAuth code exchange failed")
        return False
    return _user_authorized_for_installation(exchanged[1], installation_id)


def _state_serializer() -> URLSafeTimedSerializer:
    """Build the timed serializer used for the install state token.

    Bound to ``FLASK_SECRET_KEY`` so the same key that authenticates Flask
    sessions also authenticates install state. Created on each call so a
    rotated secret takes effect without restarting the worker.
    """
    secret = os.getenv("FLASK_SECRET_KEY") or flask.current_app.secret_key
    if not secret:
        raise RuntimeError(
            "FLASK_SECRET_KEY is not configured; cannot sign GitHub App install state"
        )
    return URLSafeTimedSerializer(secret, salt=_INSTALL_STATE_SALT)


def _sign_install_state(user_id: str) -> str:
    """Return a signed, expiring state token bound to ``user_id``."""
    return _state_serializer().dumps(user_id)


def _verify_install_state(state: str) -> str | None:
    """Return the bound ``user_id`` if ``state`` is a valid, unexpired token; else ``None``.

    Returns ``None`` (instead of raising) so the caller can render a single
    error template without leaking which check failed (signature vs. expiry
    vs. parse), which matches the rest of this module's anti-spoofing posture.
    """
    try:
        return _state_serializer().loads(state, max_age=_INSTALL_STATE_TTL_SEC)
    except SignatureExpired:
        logger.warning("[GITHUB-APP-CALLBACK] install state expired")
        return None
    except BadSignature:
        logger.warning("[GITHUB-APP-CALLBACK] install state failed signature check")
        return None
    except Exception:
        logger.warning("[GITHUB-APP-CALLBACK] install state could not be parsed")
        return None


def _render_error(reason: str) -> flask.Response:
    """Render the shared error template with a hard-coded reason string."""
    return flask.make_response(
        flask.render_template(
            "github_callback_error.html",
            error=reason,
            frontend_url=FRONTEND_URL,
        )
    )


_RECONCILE_TTL_SEC = 30
_RECONCILE_EVICT_AFTER_SEC = _RECONCILE_TTL_SEC * 10  # well past usefulness
_reconcile_last_run: dict[str, float] = {}
_reconcile_in_flight: set[str] = set()
_reconcile_lock = threading.Lock()


def _reconcile_try_claim(user_id: str, now: float) -> bool:
    """Atomic throttle + singleflight claim. Caller must pair with `_reconcile_release`."""
    cutoff = now - _RECONCILE_EVICT_AFTER_SEC
    with _reconcile_lock:
        expired = [uid for uid, ts in _reconcile_last_run.items() if ts < cutoff]
        for uid in expired:
            _reconcile_last_run.pop(uid, None)
        if user_id in _reconcile_in_flight:
            return False
        last = _reconcile_last_run.get(user_id)
        if last is not None and now - last < _RECONCILE_TTL_SEC:
            return False
        _reconcile_in_flight.add(user_id)
    return True


def _reconcile_release(user_id: str, now: float, mark_success: bool) -> None:
    """Release the singleflight claim; stamp TTL when the run was authoritative."""
    with _reconcile_lock:
        _reconcile_in_flight.discard(user_id)
        if mark_success:
            _reconcile_last_run[user_id] = now


def _reconcile_user_installations(user_id: str) -> None:
    """Soft-delete linked installs that GitHub no longer knows about (per-user TTL throttled)."""
    if not flask.current_app.config.get("GITHUB_APP_ENABLED"):
        return

    now = time.monotonic()
    if not _reconcile_try_claim(user_id, now):
        return

    mark_success = False
    try:
        try:
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """SELECT installation_id
                             FROM user_github_installations
                            WHERE user_id = %s
                              AND disconnected_at IS NULL""",
                        (user_id,),
                    )
                    linked_ids = [r[0] for r in cur.fetchall()]
        except Exception:
            logger.exception(
                "[GITHUB-APP-RECONCILE] DB read failed for user=%s", user_id,
            )
            return

        if not linked_ids:
            mark_success = True
            return

        try:
            app_jwt = mint_app_jwt()
        except GitHubAppJWTError:
            logger.warning(
                "[GITHUB-APP-RECONCILE] JWT mint failed; skipping reconcile for user=%s",
                user_id,
            )
            return

        headers = {
            "Authorization": f"Bearer {app_jwt}",
            "Accept": _GH_JSON_MEDIA_TYPE,
            "X-GitHub-Api-Version": "2022-11-28",
        }

        stale: list[int] = []
        gh_responsive = False
        gh_unreachable = False
        for iid in linked_ids:
            try:
                resp = requests.get(
                    f"https://api.github.com/app/installations/{iid}",
                    headers=headers,
                    timeout=GITHUB_RECONCILE_TIMEOUT,
                )
            except requests.RequestException:
                gh_unreachable = True
                break
            if resp.status_code == 404:
                stale.append(iid)
                gh_responsive = True
            elif resp.status_code == 200:
                gh_responsive = True

        if gh_unreachable and not gh_responsive:
            mark_success = True
            return

        if not stale:
            mark_success = gh_responsive
            return

        try:
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """UPDATE user_github_installations
                              SET disconnected_at = NOW(),
                                  repos_imported_at = NULL,
                                  repos_import_legacy = FALSE
                            WHERE user_id = %s
                              AND installation_id = ANY(%s)
                              AND disconnected_at IS NULL""",
                        (user_id, stale),
                    )
                    cur.execute(
                        """UPDATE connected_repos
                              SET installation_id = NULL,
                                  updated_at = NOW()
                            WHERE user_id = %s
                              AND installation_id = ANY(%s)""",
                        (user_id, stale),
                    )
                    conn.commit()
            mark_success = True
            logger.info(
                "[GITHUB-APP-RECONCILE] soft-deleted %d stale install link(s) for user=%s",
                len(stale), user_id,
            )
        except Exception:
            logger.exception(
                "[GITHUB-APP-RECONCILE] DB write failed for user=%s", user_id,
            )
    finally:
        _reconcile_release(user_id, now, mark_success)


@github_app_bp.route("/app/install", methods=["GET", "OPTIONS"])
@require_permission("connectors", "write")
def github_app_install_url(user_id):
    """Return the GitHub App install URL for the authenticated user.

    The frontend opens the returned ``install_url`` in a popup; GitHub then
    redirects the user back to ``/github/app/install/callback`` with
    ``state=<user_id>`` and ``installation_id`` query params.
    """
    if not flask.current_app.config.get("GITHUB_APP_ENABLED"):
        return jsonify({"error": "GitHub App not configured. Aurora is in OAuth-only mode."}), 503
    slug = (os.getenv("NEXT_PUBLIC_GITHUB_APP_SLUG") or "").strip()
    if not slug:
        # 503 (not 500) so the frontend can show a "GitHub App not yet
        # configured by your admin" affordance instead of a generic crash.
        logger.error(
            "[GITHUB-APP-INSTALL] slug not configured (NEXT_PUBLIC_GITHUB_APP_SLUG missing)"
        )
        return jsonify({"error": _APP_NOT_CONFIGURED}), 503

    try:
        signed_state = _sign_install_state(user_id)
    except RuntimeError:
        logger.exception("[GITHUB-APP-INSTALL] failed to sign install state")
        return jsonify({"error": "GitHub App install state could not be initialized"}), 500

    install_url = (
        f"https://github.com/apps/{slug}/installations/new?state={signed_state}"
    )
    return jsonify({"install_url": install_url})


@github_app_bp.route("/app/install/callback", methods=["GET"])
def github_app_install_callback():
    """Public callback hit by GitHub after a user installs the App.

    GitHub appends ``installation_id``, ``setup_action``, and ``state`` to
    the redirect URL. We MUST verify the ``installation_id`` against the
    GitHub API before persisting anything (anti-spoofing invariant #1).
    """
    if not flask.current_app.config.get("GITHUB_APP_ENABLED"):
        return jsonify({"error": "GitHub App not configured. Aurora is in OAuth-only mode."}), 503
    installation_id_raw = (request.args.get("installation_id") or "").strip()
    state = (request.args.get("state") or "").strip()
    setup_action = (request.args.get("setup_action") or "").strip()

    if not installation_id_raw or not state:
        logger.warning("[GITHUB-APP-CALLBACK] missing required params")
        return _render_error(_ERROR_MISSING_PARAMS)

    try:
        installation_id = int(installation_id_raw)
    except ValueError:
        # Spoofed/malformed installation_id - never echo the raw value.
        logger.warning("[GITHUB-APP-CALLBACK] non-integer installation_id rejected")
        return _render_error(_ERROR_BAD_INSTALL_ID)

    if installation_id <= 0:
        logger.warning("[GITHUB-APP-CALLBACK] non-positive installation_id rejected")
        return _render_error(_ERROR_BAD_INSTALL_ID)

    # Verify the signed state token BEFORE the GitHub API call. The state
    # MUST be a token signed by ``_sign_install_state`` for the user that
    # initiated the install — a raw user_id would let any caller link an
    # arbitrary installation to a victim account.
    user_id = _verify_install_state(state)
    if user_id is None:
        return _render_error(_ERROR_INVALID_STATE)
    if not validate_user_exists(user_id):
        logger.warning("[GITHUB-APP-CALLBACK] state user no longer exists")
        return _render_error(_ERROR_UNKNOWN_USER)

    # Mint the App JWT and call GitHub to verify the installation_id is real
    # AND owned by this app. A 404 from GitHub means the installation_id is
    # spoofed (or never existed for this app).
    try:
        app_jwt = mint_app_jwt()
    except GitHubAppJWTError as exc:
        logger.exception(
            "[GITHUB-APP-CALLBACK] JWT mint failed: %s", type(exc).__name__
        )
        return _render_error(_ERROR_NOT_CONFIGURED)

    api_url = f"https://api.github.com/app/installations/{installation_id}"
    headers = {
        "Authorization": f"Bearer {app_jwt}",
        "Accept": _GH_JSON_MEDIA_TYPE,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        resp = requests.get(api_url, headers=headers, timeout=GITHUB_TIMEOUT)
    except requests.RequestException as exc:
        logger.exception(
            "[GITHUB-APP-CALLBACK] GitHub API request failed: %s",
            type(exc).__name__,
        )
        return _render_error(_ERROR_GITHUB_API)

    if resp.status_code == 404:
        # Installation_id does not exist for this app - definitive proof of
        # a spoofed callback. Insert ZERO rows.
        logger.warning(
            "[GITHUB-APP-CALLBACK] GitHub returned 404 for installation"
        )
        return _render_error(_ERROR_BAD_INSTALL_ID)

    if resp.status_code != 200:
        logger.error(
            "[GITHUB-APP-CALLBACK] GitHub returned non-200: status=%d",
            resp.status_code,
        )
        return _render_error(_ERROR_GITHUB_API)

    try:
        data = resp.json()
    except ValueError as exc:
        logger.exception(
            "[GITHUB-APP-CALLBACK] GitHub response not JSON: %s",
            type(exc).__name__,
        )
        return _render_error(_ERROR_GITHUB_API)

    if not isinstance(data, dict):
        logger.error("[GITHUB-APP-CALLBACK] GitHub response not a dict")
        return _render_error(_ERROR_GITHUB_API)

    account = data.get("account") or {}
    if not isinstance(account, dict):
        account = {}

    account_login = account.get("login")
    account_id = account.get("id")
    account_type = account.get("type")

    target_type = data.get("target_type") or account_type
    permissions = data.get("permissions") or {}
    events = data.get("events") or []
    repository_selection = data.get("repository_selection") or "selected"
    suspended_at = data.get("suspended_at")  # ISO timestamp or None

    # Sanity check: required fields present and types valid.
    if (
        not isinstance(account_login, str)
        or not isinstance(account_id, int)
        or not isinstance(account_type, str)
        or not isinstance(target_type, str)
        or not isinstance(permissions, dict)
        or not isinstance(events, list)
        or not isinstance(repository_selection, str)
    ):
        logger.error("[GITHUB-APP-CALLBACK] GitHub response missing/invalid fields")
        return _render_error(_ERROR_GITHUB_API)

    # Schema CHECK constraint enforces account_type IN ('User', 'Organization').
    # Reject up-front with a clear log instead of letting psycopg raise.
    if account_type not in ("User", "Organization"):
        logger.error(
            "[GITHUB-APP-CALLBACK] unexpected account_type=%s", account_type
        )
        return _render_error(_ERROR_GITHUB_API)

    # The state proves which Aurora user clicked Install, not that the
    # installation_id in the URL is theirs. When GitHub also sent an OAuth
    # code, confirm the installer can access that installation.
    if not _installer_can_access(request.args.get("code"), installation_id):
        return _render_error(_ERROR_INSTALL_NOT_AUTHORIZED)

    # Best-effort org_id population — defensive, in case
    # ``user_github_installations`` is later promoted to RLS-protected.
    # Missing org_id is non-fatal at install time.
    org_id = get_org_id_for_user(user_id)

    # UPSERT installation + INSERT join atomically (single tx).
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                from routes.github.github_signup import _linked_elsewhere

                if _linked_elsewhere(cur, installation_id, org_id):
                    logger.warning(
                        "[GITHUB-APP-CALLBACK] installation_id=%d linked to another org",
                        installation_id,
                    )
                    return _render_error(_ERROR_LINKED_ELSEWHERE)
                cur.execute(
                    """INSERT INTO github_installations (
                            installation_id, account_login, account_id, account_type,
                            target_type, permissions, events, repository_selection,
                            suspended_at, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, NOW())
                       ON CONFLICT (installation_id) DO UPDATE SET
                            account_login = EXCLUDED.account_login,
                            account_id = EXCLUDED.account_id,
                            account_type = EXCLUDED.account_type,
                            target_type = EXCLUDED.target_type,
                            permissions = EXCLUDED.permissions,
                            events = EXCLUDED.events,
                            repository_selection = EXCLUDED.repository_selection,
                            suspended_at = EXCLUDED.suspended_at,
                            updated_at = NOW()""",
                    (
                        installation_id,
                        account_login,
                        account_id,
                        account_type,
                        target_type,
                        json.dumps(permissions),
                        json.dumps(events),
                        repository_selection,
                        suspended_at,
                    ),
                )
                cur.execute(
                    """INSERT INTO user_github_installations
                            (user_id, org_id, installation_id)
                       VALUES (%s, %s, %s)
                       ON CONFLICT (user_id, installation_id) DO UPDATE SET
                            disconnected_at = NULL,
                            org_id = EXCLUDED.org_id""",
                    (user_id, org_id, installation_id),
                )
                conn.commit()
    except Exception:
        # Don't leak details (could include payload fragments). Log here for
        # ops; return generic error template to caller.
        logger.exception(
            "[GITHUB-APP-CALLBACK] DB write failed for installation_id=%d user=%s",
            installation_id,
            user_id,
        )
        return _render_error(_ERROR_INTERNAL)

    # Deliberately log only stable identifiers, not full installation metadata.
    logger.info(
        "[GITHUB-APP-CALLBACK] linked installation_id=%d to user=%s setup_action=%s",
        installation_id,
        user_id,
        setup_action or "unknown",
    )

    # Auto-import the repos the user already granted on GitHub so they don't
    # have to re-select them inside Aurora, with PR review on for the rows the
    # import creates (existing per-repo choices are kept). Best-effort + async:
    # a failure here must never break the install (the manual picker remains
    # as a fallback).
    try:
        from routes.github.github_repo_metadata import import_installation_repos

        import_installation_repos.delay(
            user_id, installation_id, enroll_change_gating=True
        )
    except Exception:
        # Best-effort: the manual repo picker remains a fallback. Log with a
        # trace so a persistent broker/import failure is visible in ops.
        logger.warning(
            "[GITHUB-APP-CALLBACK] failed to enqueue repo auto-import installation_id=%d",
            installation_id,
            exc_info=True,
        )

    from services.onboarding.tasks import kick_org
    kick_org(org_id)

    # Ensure tool permissions include GitHub tools for this org (idempotent).
    try:
        from utils.auth.tool_registry import seed_org_tool_permissions
        seed_org_tool_permissions(org_id, user_id)
    except Exception:
        logger.warning(
            "[GITHUB-APP-CALLBACK] failed to seed tool permissions",
            exc_info=True,
        )

    # Reuse the OAuth success template. App-mode has no user token to relay,
    # so token is empty; account_login takes the github_username slot so the
    # postMessage to the parent window still carries a useful identifier.
    return flask.make_response(
        flask.render_template(
            "github_callback_success.html",
            token="",
            github_username=account_login,
            frontend_url=FRONTEND_URL,
        )
    )


@github_app_bp.route("/app/installations", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def github_app_list_installations(user_id):
    """List GitHub App installations linked to the requesting user (with reconcile)."""
    _reconcile_user_installations(user_id)

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT gi.installation_id, gi.account_login, gi.account_id,
                              gi.account_type, gi.target_type, gi.permissions,
                              gi.events, gi.repository_selection, gi.suspended_at,
                              gi.permissions_pending_update, ugi.linked_at,
                              ugi.is_primary
                         FROM user_github_installations ugi
                         JOIN github_installations gi
                              ON gi.installation_id = ugi.installation_id
                        WHERE ugi.user_id = %s
                          AND ugi.disconnected_at IS NULL
                        ORDER BY ugi.linked_at""",
                    (user_id,),
                )
                rows = cur.fetchall()
    except Exception:
        logger.exception(
            "[GITHUB-APP-LIST] DB read failed for user=%s", user_id,
        )
        return jsonify({"error": "Failed to list installations"}), 500

    installations = [
        {
            "installation_id": r[0],
            "account_login": r[1],
            "account_id": r[2],
            "account_type": r[3],
            "target_type": r[4],
            "permissions": r[5],
            "events": r[6],
            "repository_selection": r[7],
            "suspended_at": r[8].isoformat() if r[8] else None,
            "permissions_pending_update": r[9],
            "linked_at": r[10].isoformat() if r[10] else None,
            "is_primary": r[11],
        }
        for r in rows
    ]
    return jsonify({"installations": installations})


@github_app_bp.route("/app/discover-installations", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def github_app_discover_installations(user_id):
    """List App installations that exist on GitHub but aren't linked here.

    Use case: the user installed the App on their GitHub side previously,
    then disconnected on Aurora (which hard-deleted the link before
    feat/github-app-only#fix(soft-delete)). The Install GitHub App popup
    no longer redirects with a state token because the App is already
    installed, so the install/callback path can't relink them.

    This endpoint mints the App JWT, calls ``/app/installations`` to get
    every install GitHub knows about for this App, and filters out the
    ones the user already has a non-disconnected row for. Frontend
    renders the result as a "Found existing installation(s) — claim
    yours" picker. Claim is a separate POST so the user explicitly
    asserts ownership (no implicit auto-link).
    """
    if not flask.current_app.config.get("GITHUB_APP_ENABLED"):
        return jsonify({"error": _APP_NOT_CONFIGURED}), 503

    requesting_org_id = get_org_id_for_user(user_id)
    if not requesting_org_id:
        return jsonify({
            "error": "install discovery requires a user with an org context",
            "code": "MISSING_ORG_CONTEXT",
        }), 403

    try:
        app_jwt = mint_app_jwt()
    except GitHubAppJWTError:
        logger.exception("[GITHUB-APP-DISCOVER] JWT mint failed")
        return jsonify({"error": _APP_NOT_CONFIGURED}), 503

    headers = {
        "Authorization": f"Bearer {app_jwt}",
        "Accept": _GH_JSON_MEDIA_TYPE,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    next_url = "https://api.github.com/app/installations?per_page=100"
    installs: list = []
    pages_seen = 0
    MAX_PAGES = 20  # 20 * 100 = 2000 installs is plenty for Aurora scale
    while next_url and pages_seen < MAX_PAGES:
        try:
            resp = requests.get(next_url, headers=headers, timeout=GITHUB_TIMEOUT)
        except requests.RequestException:
            logger.exception("[GITHUB-APP-DISCOVER] GitHub API request failed")
            return jsonify({"error": "Failed to reach GitHub"}), 502

        if resp.status_code != 200:
            logger.error(
                "[GITHUB-APP-DISCOVER] GitHub returned status=%d", resp.status_code
            )
            return jsonify({"error": "Failed to list App installations"}), 502

        try:
            page = resp.json()
        except ValueError:
            logger.error("[GITHUB-APP-DISCOVER] response not JSON")
            return jsonify({"error": "Failed to parse GitHub response"}), 502

        if not isinstance(page, list):
            page = []
        installs.extend(page)
        pages_seen += 1

        next_url = None
        link_header = resp.headers.get("Link", "")
        if link_header:
            for part in link_header.split(","):
                segment = part.strip()
                if segment.endswith('rel="next"'):
                    url_part = segment.split(";", 1)[0].strip()
                    if url_part.startswith("<") and url_part.endswith(">"):
                        next_url = url_part[1:-1]
                    break

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT installation_id, org_id
                         FROM user_github_installations
                        WHERE disconnected_at IS NULL""",
                )
                bindings: dict[int, set[str | None]] = {}
                for inst_id, bound_org in cur.fetchall():
                    bindings.setdefault(inst_id, set()).add(bound_org)
                # Installs this user disconnected, while this org held them
                # last, can be relinked: the App stays installed on GitHub,
                # so the install popup has nothing to send back.
                for inst_id in _relinkable_installs(cur, user_id, requesting_org_id):
                    bindings.setdefault(inst_id, {requesting_org_id})
                cur.execute(
                    """SELECT installation_id
                         FROM user_github_installations
                        WHERE user_id = %s
                          AND disconnected_at IS NULL""",
                    (user_id,),
                )
                already_linked_to_user = {r[0] for r in cur.fetchall()}
    except Exception:
        logger.exception("[GITHUB-APP-DISCOVER] DB read failed")
        return jsonify({"error": "Failed to check existing links"}), 500

    out = []
    for inst in installs:
        if not isinstance(inst, dict):
            continue
        inst_id = inst.get("id")
        if not isinstance(inst_id, int):
            continue
        if inst_id in already_linked_to_user:
            continue
        owning_orgs = bindings.get(inst_id, set())
        if not owning_orgs:
            continue
        if requesting_org_id not in owning_orgs:
            continue
        account = inst.get("account") or {}
        out.append(
            {
                "installation_id": inst_id,
                "account_login": account.get("login") if isinstance(account, dict) else None,
                "account_type": account.get("type") if isinstance(account, dict) else None,
                "repository_selection": inst.get("repository_selection"),
                "suspended_at": inst.get("suspended_at"),
            }
        )
    return jsonify({"installations": out})


@github_app_bp.route(
    "/app/installations/<int:installation_id>/claim", methods=["POST"]
)
@require_permission("connectors", "write")
def github_app_claim_installation(user_id, installation_id):
    """Link an existing App installation to the current Aurora user."""
    if not flask.current_app.config.get("GITHUB_APP_ENABLED"):
        return jsonify({"error": _APP_NOT_CONFIGURED}), 503

    requesting_org_id = get_org_id_for_user(user_id)
    if not requesting_org_id:
        return jsonify({
            "error": "install claim requires a user with an org context",
            "code": "MISSING_ORG_CONTEXT",
        }), 403

    owning_orgs = _orgs_owning_install(installation_id)
    foreign_orgs = owning_orgs - {requesting_org_id}
    if foreign_orgs:
        logger.warning(
            "[GITHUB-APP-CLAIM] cross-org claim refused user=%s installation_id=%d",
            user_id, installation_id,
        )
        return jsonify({
            "error": "install is already linked to a different organization",
            "code": "CROSS_ORG_CLAIM_REFUSED",
        }), 403
    if requesting_org_id not in owning_orgs and not _can_relink(
        user_id, requesting_org_id, installation_id
    ):
        # Claim joins an install a teammate linked, or relinks one this user
        # disconnected while their org held it last (what discovery lists).
        logger.warning(
            "[GITHUB-APP-CLAIM] claim of unlinked install refused user=%s installation_id=%d",
            user_id, installation_id,
        )
        return jsonify({
            "error": "install is not linked to your organization; install it from Connectors",
            "code": "INSTALL_NOT_IN_ORG",
        }), 403

    try:
        app_jwt = mint_app_jwt()
    except GitHubAppJWTError:
        logger.exception("[GITHUB-APP-CLAIM] JWT mint failed")
        return jsonify({"error": _APP_NOT_CONFIGURED}), 503

    try:
        resp = requests.get(
            f"https://api.github.com/app/installations/{installation_id}",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": _GH_JSON_MEDIA_TYPE,
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=GITHUB_TIMEOUT,
        )
    except requests.RequestException:
        logger.exception(
            "[GITHUB-APP-CLAIM] GitHub API request failed user=%s installation_id=%d",
            user_id, installation_id,
        )
        return jsonify({"error": "Failed to verify installation with GitHub"}), 502

    if resp.status_code == 404:
        return jsonify({"error": "Installation not found"}), 404

    if resp.status_code != 200:
        logger.error(
            "[GITHUB-APP-CLAIM] GitHub returned status=%d for installation_id=%d",
            resp.status_code, installation_id,
        )
        return jsonify({"error": "Failed to verify installation"}), 502

    try:
        data = resp.json()
    except ValueError:
        return jsonify({"error": "Failed to parse GitHub response"}), 502

    if not isinstance(data, dict):
        logger.error(
            "[GITHUB-APP-CLAIM] GitHub response is not an object (got %s)",
            type(data).__name__,
        )
        return jsonify({"error": "Unexpected GitHub response shape"}), 502

    account = data.get("account") or {}
    if not isinstance(account, dict):
        account = {}
    account_login = account.get("login")
    account_id = account.get("id")
    account_type = account.get("type")
    target_type = data.get("target_type") or account_type
    permissions = data.get("permissions") or {}
    events = data.get("events") or []
    repository_selection = data.get("repository_selection") or "selected"
    suspended_at = data.get("suspended_at")

    if not isinstance(account_login, str) or not isinstance(account_type, str):
        logger.error("[GITHUB-APP-CLAIM] GitHub response missing fields")
        return jsonify({"error": "Invalid response from GitHub"}), 502

    if account_type not in ("User", "Organization"):
        return jsonify({"error": "Unexpected account_type"}), 502

    org_id = get_org_id_for_user(user_id)

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                # Lock first, like every other link path.
                from routes.github.github_signup import _linked_elsewhere

                if _linked_elsewhere(cur, installation_id, org_id):
                    return jsonify({
                        "error": "install is already linked to a different organization",
                        "code": "CROSS_ORG_CLAIM_REFUSED",
                    }), 403
                cur.execute(
                    """INSERT INTO github_installations (
                            installation_id, account_login, account_id, account_type,
                            target_type, permissions, events, repository_selection,
                            suspended_at, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, NOW())
                       ON CONFLICT (installation_id) DO UPDATE SET
                            account_login = EXCLUDED.account_login,
                            account_id = EXCLUDED.account_id,
                            account_type = EXCLUDED.account_type,
                            target_type = EXCLUDED.target_type,
                            permissions = EXCLUDED.permissions,
                            events = EXCLUDED.events,
                            repository_selection = EXCLUDED.repository_selection,
                            suspended_at = EXCLUDED.suspended_at,
                            updated_at = NOW()""",
                    (
                        installation_id,
                        account_login,
                        account_id,
                        account_type,
                        target_type,
                        json.dumps(permissions),
                        json.dumps(events),
                        repository_selection,
                        suspended_at,
                    ),
                )
                cur.execute(
                    """INSERT INTO user_github_installations
                            (user_id, org_id, installation_id)
                       VALUES (%s, %s, %s)
                       ON CONFLICT (user_id, installation_id) DO UPDATE SET
                            disconnected_at = NULL,
                            org_id = EXCLUDED.org_id""",
                    (user_id, org_id, installation_id),
                )
                conn.commit()
    except Exception:
        logger.exception(
            "[GITHUB-APP-CLAIM] DB write failed user=%s installation_id=%d",
            user_id, installation_id,
        )
        return jsonify({"error": "Failed to link installation"}), 500

    logger.info(
        "[GITHUB-APP-CLAIM] user=%s claimed installation_id=%d account=%s",
        user_id, installation_id, account_login,
    )
    _enqueue_enrolled_import(user_id, installation_id, "[GITHUB-APP-CLAIM]")
    return jsonify({"success": True, "installation_id": installation_id})


def _enqueue_enrolled_import(user_id: str, installation_id: int, log_prefix: str) -> None:
    """Import a just-linked installation's repos with PR review on (best-effort)."""
    try:
        from routes.github.github_repo_metadata import import_installation_repos

        import_installation_repos.delay(
            user_id, installation_id, enroll_change_gating=True
        )
    except Exception:
        logger.warning(
            "%s failed to enqueue repo auto-import installation_id=%d",
            log_prefix, installation_id, exc_info=True,
        )


def _claim_refusal(cur, user_id: str, claim: dict) -> dict | None:
    """Why ``user_id`` may not redeem ``claim``, or None when it may."""
    cur.execute(
        "SELECT LOWER(email), github_user_id FROM users WHERE id = %s", (user_id,)
    )
    row = cur.fetchone()
    if not row or row[0] != claim["email"]:
        logger.warning("[GITHUB-APP-CLAIM-PENDING] email mismatch user=%s", user_id)
        return {"error": "Signed in as a different account", "code": "CLAIM_EMAIL_MISMATCH"}
    if row[1] is not None and row[1] != claim["gh_id"]:
        return {"error": "Account is linked to another GitHub user",
                "code": "CLAIM_IDENTITY_MISMATCH"}
    cur.execute(
        "SELECT 1 FROM users WHERE github_user_id = %s AND id <> %s",
        (claim["gh_id"], user_id),
    )
    if cur.fetchone():
        return {"error": "GitHub user is linked to another account",
                "code": "CLAIM_IDENTITY_MISMATCH"}
    return None


@github_app_bp.route("/app/claim-pending", methods=["POST", "OPTIONS"])
@require_permission("connectors", "write")
def github_app_claim_pending(user_id):
    """Finish a one-click install whose GitHub email matched an existing account.

    The signup callback verified the installation, the installer's identity
    and their access to it, then sent them to sign in with a signed claim
    token instead of logging them in (an email match alone must never be a
    login). Having now signed in, the account is linked when its email is
    the one GitHub reported for the installer.
    """
    from routes.github.github_signup import _verify_installation, verify_claim_token

    if not flask.current_app.config.get("GITHUB_APP_ENABLED"):
        return jsonify({"error": _APP_NOT_CONFIGURED}), 503

    body = request.get_json(silent=True) or {}
    token = body.get("token") if isinstance(body, dict) else None
    claim = verify_claim_token(token) if isinstance(token, str) else None
    if claim is None:
        return jsonify({"error": "Claim link expired", "code": "CLAIM_INVALID"}), 400

    installation_id = claim["iid"]
    org_id = get_org_id_for_user(user_id)
    if not org_id:
        return jsonify({"error": "No organization", "code": "MISSING_ORG_CONTEXT"}), 403
    if _orgs_owning_install(installation_id) - {org_id}:
        logger.warning(
            "[GITHUB-APP-CLAIM-PENDING] cross-org claim refused user=%s installation_id=%d",
            user_id, installation_id,
        )
        return jsonify({"error": "Installation belongs to another organization",
                        "code": "CROSS_ORG_CLAIM_REFUSED"}), 403

    install_data = _verify_installation(installation_id)
    if install_data is None or install_data.get("id") != installation_id:
        return jsonify({"error": "Could not verify installation with GitHub"}), 502
    account = install_data.get("account") if isinstance(install_data.get("account"), dict) else {}
    if account.get("type") not in ("User", "Organization") or not isinstance(
        account.get("login"), str
    ):
        return jsonify({"error": "Invalid response from GitHub"}), 502

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                refusal = _claim_refusal(cur, user_id, claim)
                if refusal is not None:
                    return jsonify(refusal), 403
                from routes.github.github_signup import _linked_elsewhere

                if _linked_elsewhere(cur, installation_id, org_id):
                    return jsonify({"error": "Installation belongs to another organization",
                                        "code": "CROSS_ORG_CLAIM_REFUSED"}), 403
                # Remember the GitHub identity so the next one-click install
                # logs this person straight in.
                cur.execute(
                    "UPDATE users SET github_user_id = %s, github_login = %s WHERE id = %s",
                    (claim["gh_id"], claim["gh_login"], user_id),
                )
                from routes.github.github_signup import _link_installation

                _link_installation(cur, user_id, org_id, install_data)
                conn.commit()
    except Exception:
        logger.exception(
            "[GITHUB-APP-CLAIM-PENDING] DB write failed user=%s installation_id=%d",
            user_id, installation_id,
        )
        return jsonify({"error": "Failed to link installation"}), 500

    logger.info(
        "[GITHUB-APP-CLAIM-PENDING] user=%s linked installation_id=%d",
        user_id, installation_id,
    )
    _enqueue_enrolled_import(user_id, installation_id, "[GITHUB-APP-CLAIM-PENDING]")
    try:
        from utils.auth.tool_registry import seed_org_tool_permissions

        seed_org_tool_permissions(org_id, user_id)
    except Exception:
        logger.warning("[GITHUB-APP-CLAIM-PENDING] tool permission seeding failed", exc_info=True)
    return jsonify({"success": True, "installation_id": installation_id})


_LINK_TICKET_SALT = "aurora.github.app.link-pending-ticket.v2"
_LINK_TICKET_TTL_SEC = 2 * 60
_LINK_STATE_SALT = "aurora.github.app.link-pending-state.v1"
_LINK_STATE_TTL_SEC = 10 * 60


def _token_hash(token: str) -> str:
    import hashlib

    return hashlib.sha256(token.encode()).hexdigest()


def _sign_link(salt: str, token: str, user_id: str, org_id: str) -> str:
    from routes.github.github_signup import _state_serializer

    return _state_serializer(salt).dumps({"t": _token_hash(token), "u": user_id, "o": org_id})


def _verify_link(salt: str, max_age: int, signed, token: str) -> tuple[str, str] | None:
    from routes.github.github_signup import _state_serializer

    if not isinstance(signed, str) or not signed:
        return None
    try:
        payload = _state_serializer(salt).loads(signed, max_age=max_age)
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("t") != _token_hash(token):
        return None
    user_id, org_id = payload.get("u"), payload.get("o")
    if not isinstance(user_id, str) or not isinstance(org_id, str):
        return None
    return user_id, org_id


def sign_link_state(token: str, user_id: str, org_id: str) -> str:
    """Issued by the card to the signed-in session that is about to click Connect."""
    return _sign_link(_LINK_STATE_SALT, token, user_id, org_id)


def verify_link_state(state, token: str) -> tuple[str, str] | None:
    return _verify_link(_LINK_STATE_SALT, _LINK_STATE_TTL_SEC, state, token)


def sign_link_ticket(token: str, user_id: str, org_id: str) -> str:
    """Short-lived proof that this user's Connect was confirmed in the installer's browser."""
    return _sign_link(_LINK_TICKET_SALT, token, user_id, org_id)


def verify_link_ticket(ticket, token: str) -> tuple[str, str] | None:
    return _verify_link(_LINK_TICKET_SALT, _LINK_TICKET_TTL_SEC, ticket, token)


@github_app_bp.route("/app/link-pending/start", methods=["POST", "OPTIONS"])
@require_permission("connectors", "write")
def github_app_link_pending_start(user_id):
    """What the "Connect GitHub to <org>?" card shows, read from the signed token.

    Also issues the link state that Connect carries through the installer's
    browser, so the link can only finish for this user and org.
    """
    from routes.github.github_signup import verify_pending_signup
    from utils.auth.stateless_auth import get_org_id_from_request

    body = request.get_json(silent=True) or {}
    token = body.get("token") if isinstance(body, dict) else None
    pending = verify_pending_signup(token)
    if pending is None:
        return jsonify({"error": "Install link expired", "code": "CLAIM_INVALID"}), 400
    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No organization context", "code": "MISSING_ORG_CONTEXT"}), 403
    return jsonify({
        "account_login": pending.get("acct") or "",
        "installer_login": pending["identity"]["login"],
        "link_state": sign_link_state(token, user_id, org_id),
    })


@github_app_bp.route("/app/link-pending/finish", methods=["POST", "OPTIONS"])
@require_permission("connectors", "write")
def github_app_link_pending_finish(user_id):
    """Connect: link the pending install to the signed-in user's org.

    Needs the ticket ``/github/app/link-pending/confirm`` hands out only to
    the browser that did the install, in exchange for the link state this
    user's card was issued. The ticket names that user and org, and must
    match the session finishing it: a ticket the installer minted for
    themselves cannot link their install into someone else's org.
    """
    from routes.github.github_signup import (
        release_pending_signup,
        reserve_pending_signup,
        verify_pending_signup,
    )
    from utils.auth.stateless_auth import get_org_id_from_request

    body = request.get_json(silent=True) or {}
    token = body.get("token") if isinstance(body, dict) else None
    ticket = body.get("ticket") if isinstance(body, dict) else None
    pending = verify_pending_signup(token)
    bound = verify_link_ticket(ticket, token) if pending is not None else None
    org_id = get_org_id_from_request()
    if bound is None or bound != (user_id, org_id):
        return jsonify({"error": "Install link expired", "code": "CLAIM_INVALID"}), 400
    if not reserve_pending_signup(token):
        return jsonify({"error": "Install link already used", "code": "CLAIM_INVALID"}), 400
    error = link_pending_install(user_id, org_id, pending)
    if error is not None:
        release_pending_signup(token)
        return jsonify({"error": error, "code": "LINK_FAILED"}), 409
    return jsonify({"success": True, "installation_id": pending["iid"]})


def link_pending_install(user_id: str, authorized_org: str, pending: dict) -> str | None:
    """Link a pending new install to ``user_id``'s org; an error message or None.

    Only the installation is linked: the GitHub identity is not attached to
    this account, so a pending install can never become a way to log in.
    """
    from routes.github.github_signup import (
        _link_installation,
        _linked_elsewhere,
        _verify_installation,
    )

    if not flask.current_app.config.get("GITHUB_APP_ENABLED"):
        return _APP_NOT_CONFIGURED
    installation_id = pending["iid"]
    if not validate_user_exists(user_id):
        return _ERROR_UNKNOWN_USER
    org_id = get_org_id_for_user(user_id)
    if not org_id or org_id != authorized_org:
        return _ERROR_UNKNOWN_USER
    install_data = _verify_installation(installation_id)
    if install_data is None or install_data.get("id") != installation_id:
        return _ERROR_GITHUB_API

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                if _linked_elsewhere(cur, installation_id, org_id):
                    return _ERROR_LINKED_ELSEWHERE
                _link_installation(cur, user_id, org_id, install_data)
                conn.commit()
    except Exception:
        logger.exception(
            "[GITHUB-APP-LINK-PENDING] DB write failed user=%s installation_id=%d",
            user_id, installation_id,
        )
        return _ERROR_INTERNAL

    logger.info(
        "[GITHUB-APP-LINK-PENDING] user=%s linked installation_id=%d",
        user_id, installation_id,
    )
    _enqueue_enrolled_import(user_id, installation_id, "[GITHUB-APP-LINK-PENDING]")
    try:
        from utils.auth.tool_registry import seed_org_tool_permissions

        seed_org_tool_permissions(org_id, user_id)
    except Exception:
        logger.warning("[GITHUB-APP-LINK-PENDING] tool permission seeding failed", exc_info=True)
    return None


@github_app_bp.route(
    "/app/installations/<int:installation_id>", methods=["DELETE"]
)
@require_permission("connectors", "write")
def github_app_unlink_installation(user_id, installation_id):
    """Remove the user->installation join row only.

    Does NOT uninstall the GitHub App from the user's GitHub account; that
    must be done by the user via GitHub's UI. Webhook handlers (Task 13)
    will reconcile if/when the user removes the install on GitHub's side.
    """
    try:
        from utils.auth.stateless_auth import set_rls_context

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                hand_off_import_state(cur, user_id, [installation_id])
                cur.execute(
                    """DELETE FROM user_github_installations
                        WHERE user_id = %s AND installation_id = %s""",
                    (user_id, installation_id),
                )
                deleted = cur.rowcount

                set_rls_context(
                    cur, conn, user_id,
                    log_prefix="[GITHUB-APP-UNLINK]",
                )
                cur.execute(
                    """UPDATE connected_repos
                          SET installation_id = NULL,
                              updated_at = NOW()
                        WHERE user_id = %s
                          AND installation_id = %s""",
                    (user_id, installation_id),
                )
                conn.commit()
    except Exception:
        logger.exception(
            "[GITHUB-APP-UNLINK] DB delete failed user=%s installation_id=%d",
            user_id, installation_id,
        )
        return jsonify({"error": "Failed to unlink installation"}), 500

    if deleted == 0:
        return jsonify({"error": "Installation link not found"}), 404

    logger.info(
        "[GITHUB-APP-UNLINK] removed user=%s installation_id=%d",
        user_id, installation_id,
    )
    return jsonify({"success": True, "installation_id": installation_id})


@github_app_bp.route("/auth-config", methods=["GET"])
@require_permission("connectors", "read")
def github_auth_config(user_id):  # noqa: ARG001 — user_id required by decorator
    """Return the deployment's GitHub auth configuration."""
    from utils.flags.feature_flags import is_incident_prevention_enabled

    app_runtime_ready = bool(flask.current_app.config.get("GITHUB_APP_ENABLED"))
    return jsonify(
        {
            "mode": get_auth_mode(),
            "app_enabled": is_app_enabled() and app_runtime_ready,
            # oauth_enabled drives the "Connect via OAuth" CTA — gate it on
            # NEW-connection enablement (off by default; OAuth is deprecated).
            "oauth_enabled": is_oauth_login_enabled(),
            "oauth_configured": oauth_credentials_configured(),
            "incident_prevention_enabled": is_incident_prevention_enabled(),
        }
    )


@github_app_bp.route("/status", methods=["GET"])
@require_permission("connectors", "read")
def github_status(user_id):
    """Connection status for the GitHub connector (hybrid-aware, with reconcile)."""
    app_runtime_ready = bool(flask.current_app.config.get("GITHUB_APP_ENABLED"))
    app_branch_active = is_app_enabled() and app_runtime_ready

    if app_branch_active:
        _reconcile_user_installations(user_id)

    app_username: str | None = None
    if app_branch_active:
        # Org-scoped: an installation linked by any org member marks the
        # connector connected, so every member sees identical state
        # (consistent with OAuth tokens and all other connectors). The
        # user's own link is preferred for the displayed account name.
        org_id = get_org_id_for_user(user_id)
        try:
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """SELECT gi.account_login
                             FROM user_github_installations ugi
                             JOIN github_installations gi
                                  ON gi.installation_id = ugi.installation_id
                            WHERE (ugi.user_id = %s OR ugi.org_id = %s)
                              AND ugi.disconnected_at IS NULL
                              AND gi.suspended_at IS NULL
                            ORDER BY (ugi.user_id = %s) DESC,
                                     ugi.is_primary DESC, ugi.linked_at DESC
                            LIMIT 1""",
                        (user_id, org_id, user_id),
                    )
                    row = cur.fetchone()
                    if row:
                        app_username = row[0]
        except Exception as exc:
            logger.exception(
                "[GITHUB-STATUS] DB read failed user=%s: %s",
                user_id, exc,
            )
            return jsonify({"connected": False, "error": "Failed to check status"}), 500

    if app_username:
        return jsonify({"connected": True, "username": app_username, "auth_method": "app"})

    # Existing OAuth connections stay valid even in App-only mode (deprecation
    # keeps them working until the user disconnects).
    if is_oauth_token_honored():
        try:
            creds = get_credentials_from_db(user_id, "github")
        except Exception as exc:
            logger.exception(
                "[GITHUB-STATUS] OAuth credential read failed user=%s: %s",
                user_id, exc,
            )
            return jsonify({"connected": False, "error": "Failed to check status"}), 500

        if creds and creds.get("access_token"):
            return jsonify(
                {
                    "connected": True,
                    "username": creds.get("username"),
                    "auth_method": "oauth",
                }
            )

    return jsonify({"connected": False})


@github_app_bp.route("/disconnect", methods=["POST"])
@require_permission("connectors", "write")
def github_disconnect(user_id):
    """Sever GitHub auth state. Optional `{"also_uninstall": true}` deletes the install on GitHub too."""
    body = request.get_json(silent=True) or {}
    also_uninstall = bool(body.get("also_uninstall"))

    linked_installs: list[int] = []
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT installation_id
                         FROM user_github_installations
                        WHERE user_id = %s
                          AND disconnected_at IS NULL""",
                    (user_id,),
                )
                linked_installs = [r[0] for r in cur.fetchall()]
    except Exception as exc:
        logger.warning(
            "[GITHUB-DISCONNECT] failed to snapshot linked installs user=%s: %s",
            user_id, exc,
        )

    uninstalled_on_github = 0
    uninstall_failures = 0
    if also_uninstall and linked_installs and flask.current_app.config.get("GITHUB_APP_ENABLED"):
        try:
            app_jwt = mint_app_jwt()
        except GitHubAppJWTError:
            logger.warning(
                "[GITHUB-DISCONNECT] App JWT mint failed; skipping GitHub-side uninstall"
            )
            app_jwt = None

        if app_jwt:
            headers = {
                "Authorization": f"Bearer {app_jwt}",
                "Accept": _GH_JSON_MEDIA_TYPE,
                "X-GitHub-Api-Version": "2022-11-28",
            }
            for iid in linked_installs:
                try:
                    resp = requests.delete(
                        f"https://api.github.com/app/installations/{iid}",
                        headers=headers,
                        timeout=GITHUB_RECONCILE_TIMEOUT,
                    )
                except requests.RequestException as exc:
                    uninstall_failures += 1
                    logger.warning(
                        "[GITHUB-DISCONNECT] uninstall request failed installation_id=%d: %s",
                        iid, type(exc).__name__,
                    )
                    continue
                if resp.status_code in (204, 404):
                    uninstalled_on_github += 1
                else:
                    uninstall_failures += 1
                    logger.warning(
                        "[GITHUB-DISCONNECT] uninstall returned status=%d installation_id=%d",
                        resp.status_code, iid,
                    )

    soft_deleted_installs = 0
    try:
        from utils.auth.stateless_auth import set_rls_context

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                from utils.auth.stateless_auth import get_org_id_from_request

                # First: set_rls_context commits, so it must run before any
                # disconnect write or it could persist half of one.
                rls_org = set_rls_context(
                    cur, conn, user_id,
                    org_id=get_org_id_from_request(),
                    log_prefix="[GITHUB-DISCONNECT]",
                )
                hand_off_import_state(cur, user_id)
                cur.execute(
                    """UPDATE user_github_installations
                          SET disconnected_at = NOW(),
                              repos_imported_at = NULL,
                              repos_import_legacy = FALSE
                        WHERE user_id = %s
                          AND disconnected_at IS NULL""",
                    (user_id,),
                )
                soft_deleted_installs = cur.rowcount

                cur.execute(
                    """SELECT 1 FROM user_github_installations
                        WHERE org_id = %s AND disconnected_at IS NULL LIMIT 1""",
                    (rls_org,),
                )
                if rls_org and cur.fetchone() is None:
                    # The org's last link is gone, so this starts over: a
                    # reconnect imports every granted repo again. Removals
                    # recorded while connected (the app clears the picker
                    # first) must not block it. Teammates still linked keep
                    # their removals.
                    cur.execute(
                        "DELETE FROM github_repo_exclusions WHERE org_id = %s",
                        (rls_org,),
                    )
                if linked_installs:
                    cur.execute(
                        """UPDATE connected_repos
                              SET installation_id = NULL,
                                  updated_at = NOW()
                            WHERE user_id = %s
                              AND installation_id = ANY(%s)""",
                        (user_id, linked_installs),
                    )
                conn.commit()
    except Exception as exc:
        logger.exception(
            "[GITHUB-DISCONNECT] DB soft-delete failed user=%s: %s",
            user_id, exc,
        )
        return jsonify({"error": "Failed to disconnect"}), 500

    oauth_removed = False
    try:
        from utils.secrets.secret_ref_utils import delete_user_secret

        success, _ = delete_user_secret(user_id, "github")
        oauth_removed = bool(success)
    except Exception as exc:
        logger.warning(
            "[GITHUB-DISCONNECT] OAuth credential delete failed user=%s: %s",
            user_id, exc,
        )

    logger.info(
        "[GITHUB-DISCONNECT] user=%s soft_deleted_installs=%d "
        "also_uninstall=%s uninstalled_on_github=%d uninstall_failures=%d",
        user_id, soft_deleted_installs,
        also_uninstall, uninstalled_on_github, uninstall_failures,
    )
    return jsonify(
        {
            "success": True,
            "removed_installations": soft_deleted_installs,
            "uninstalled_on_github": uninstalled_on_github,
            "uninstall_failures": uninstall_failures,
            "oauth_token_removed": oauth_removed,
        }
    )
