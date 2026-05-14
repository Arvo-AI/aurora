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
    is_oauth_enabled,
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

# Default to "" (not None) so Jinja never renders the string "None" into
# the success template's postMessage targetOrigin — that would throw a
# DOMException and silently break the instant-refresh path.
FRONTEND_URL = os.getenv("FRONTEND_URL") or ""
GITHUB_TIMEOUT = 20
# Tighter timeout for reconcile: this runs on the hot /github/status
# path and the user is waiting on the response. With the default 20 s,
# an unreachable GitHub could block status for ``N * 20 s`` per linked
# install. 3 s is well past p99 of a healthy ``api.github.com`` call
# while still catching real outages quickly.
GITHUB_RECONCILE_TIMEOUT = 3

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


# Per-user reconcile throttle + singleflight claim. Status /
# installations endpoints are called frequently from the UI (mount,
# focus, visibility, post-popup) and Flask runs handlers on multiple
# threads (gunicorn ``--threads N``). Without protection, a burst of
# concurrent requests would all blow past the throttle check at the
# same instant and each fire 1-N blocking GitHub calls.
#
# Two pieces under the same lock:
#   - ``_reconcile_last_run``: last successful reconcile timestamp.
#     Skipping by TTL avoids hammering GitHub during a quiet hour.
#   - ``_reconcile_in_flight``: the set of user_ids currently being
#     reconciled. Claiming the user before doing GitHub work makes
#     this a true singleflight: late-arriving threads short-circuit
#     instead of duplicating in-progress work.
#
# Lock is held only across in-memory mutations — never during the
# GitHub HTTP calls — so it can't deadlock or block other users.
_RECONCILE_TTL_SEC = 30
_RECONCILE_EVICT_AFTER_SEC = _RECONCILE_TTL_SEC * 10  # well past usefulness
_reconcile_last_run: dict[str, float] = {}
_reconcile_in_flight: set[str] = set()
_reconcile_lock = threading.Lock()


def _reconcile_try_claim(user_id: str, now: float) -> bool:
    """Atomic throttle + singleflight claim.

    Returns ``True`` when this thread acquired the right to run a
    reconcile for ``user_id``. The caller MUST then invoke
    ``_reconcile_release`` (typically in a try/finally) so subsequent
    threads can claim. Returns ``False`` when another thread is
    already reconciling this user OR when the TTL hasn't elapsed.
    """
    cutoff = now - _RECONCILE_EVICT_AFTER_SEC
    with _reconcile_lock:
        # Bounded O(N) sweep over active users.
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
    """Release the singleflight claim. Optionally stamp the TTL."""
    with _reconcile_lock:
        _reconcile_in_flight.discard(user_id)
        if mark_success:
            _reconcile_last_run[user_id] = now


def _reconcile_user_installations(user_id: str) -> None:
    """Soft-delete linked installs that GitHub no longer knows about.

    Webhook-driven cleanup of ``installation.deleted`` requires the webhook
    to actually reach Aurora. In dev — and any deployment where the public
    webhook URL is misconfigured — uninstalling the App on GitHub leaves
    Aurora's DB pointing at an installation that no longer exists. Without
    this reconciliation, the connector card would still render "Connected".

    Verifies each linked installation individually with
    ``GET /app/installations/{id}`` (one call per linked install, typically
    1-3 per user). A 404 means GitHub no longer knows about the
    installation — soft-delete via ``disconnected_at = NOW()`` so the user
    can re-claim on reinstall. Any other status (network error, 5xx, rate
    limit) leaves the row alone so a transient blip never shows a real
    connection as disconnected.

    Per-user TTL throttle prevents hammering GitHub when the UI fires a
    burst of status checks (focus + visibility + mount can all land
    within milliseconds).
    """
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
            # Empty user — fast path. Mark a successful pass so we
            # don't re-query the DB for the rest of the TTL window.
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
            "Accept": "application/vnd.github+json",
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
                # GitHub appears unreachable. No point trying the rest
                # of this user's installs; bail and let the throttle
                # carry the next ~30 s of status checks. Without this,
                # a network outage would block status endpoints for
                # ``N * timeout`` per request.
                gh_unreachable = True
                break
            if resp.status_code == 404:
                stale.append(iid)
                gh_responsive = True
            elif resp.status_code == 200:
                gh_responsive = True
            # Any other status (5xx, 401, secondary-rate-limit 403): be
            # conservative and leave the row alone. The next reconcile
            # pass will retry; ``mark_success`` stays False so a fully
            # broken GitHub doesn't suppress the next attempt.

        # When GitHub was unreachable the entire pass we still stamp
        # the throttle so the next request doesn't burn another timeout
        # on the same outage. The TTL is short enough (30 s) that
        # recovery is detected quickly.
        if gh_unreachable and not gh_responsive:
            mark_success = True
            return

        if not stale:
            # Nothing to write — the GitHub side answered cleanly so
            # we can stamp the throttle and move on.
            mark_success = gh_responsive
            return

        try:
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """UPDATE user_github_installations
                              SET disconnected_at = NOW()
                            WHERE user_id = %s
                              AND installation_id = ANY(%s)
                              AND disconnected_at IS NULL""",
                        (user_id, stale),
                    )
                    cur.execute(
                        """UPDATE github_connected_repos
                              SET installation_id = NULL,
                                  updated_at = NOW()
                            WHERE user_id = %s
                              AND installation_id = ANY(%s)""",
                        (user_id, stale),
                    )
                    conn.commit()
            # ONLY stamp the throttle after the soft-delete actually
            # landed. If the DB write failed below, ``mark_success``
            # stays False and the next status check retries instead of
            # waiting out the TTL with stale "connected" UI state.
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
        return jsonify({"error": "GitHub App not configured"}), 503

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
        logger.error(
            "[GITHUB-APP-CALLBACK] JWT mint failed: %s", type(exc).__name__
        )
        return _render_error(_ERROR_NOT_CONFIGURED)

    api_url = f"https://api.github.com/app/installations/{installation_id}"
    headers = {
        "Authorization": f"Bearer {app_jwt}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        resp = requests.get(api_url, headers=headers, timeout=GITHUB_TIMEOUT)
    except requests.RequestException as exc:
        logger.error(
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
    except (ValueError, json.JSONDecodeError) as exc:
        logger.error(
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

    # Best-effort org_id population — defensive, in case
    # ``user_github_installations`` is later promoted to RLS-protected.
    # Missing org_id is non-fatal at install time.
    org_id = get_org_id_for_user(user_id)

    # UPSERT installation + INSERT join atomically (single tx).
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
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
    """List GitHub App installations linked to the requesting user.

    Self-heals against silent uninstalls: in dev (and any environment where
    the ``installation.deleted`` webhook isn't reaching us), the user can
    uninstall the App on GitHub and the DB still shows it linked. Before
    returning, ask GitHub which installation_ids are still live for this
    App and soft-delete any of OUR linked rows that GitHub no longer
    knows about. One App-JWT API call regardless of linked count.
    """
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
        return jsonify({"error": "GitHub App not configured"}), 503

    try:
        app_jwt = mint_app_jwt()
    except GitHubAppJWTError:
        logger.exception("[GITHUB-APP-DISCOVER] JWT mint failed")
        return jsonify({"error": "GitHub App not configured"}), 503

    try:
        resp = requests.get(
            "https://api.github.com/app/installations",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=GITHUB_TIMEOUT,
        )
    except requests.RequestException:
        logger.exception("[GITHUB-APP-DISCOVER] GitHub API request failed")
        return jsonify({"error": "Failed to reach GitHub"}), 502

    if resp.status_code != 200:
        logger.error(
            "[GITHUB-APP-DISCOVER] GitHub returned status=%d", resp.status_code
        )
        return jsonify({"error": "Failed to list App installations"}), 502

    try:
        installs = resp.json()
    except ValueError:
        logger.error("[GITHUB-APP-DISCOVER] response not JSON")
        return jsonify({"error": "Failed to parse GitHub response"}), 502

    if not isinstance(installs, list):
        installs = []

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
                already_linked = {r[0] for r in cur.fetchall()}
    except Exception:
        logger.exception("[GITHUB-APP-DISCOVER] DB read failed")
        return jsonify({"error": "Failed to check existing links"}), 500

    out = []
    for inst in installs:
        if not isinstance(inst, dict):
            continue
        inst_id = inst.get("id")
        if not isinstance(inst_id, int) or inst_id in already_linked:
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
    "/app/installations/<int:installation_id>/claim", methods=["POST", "OPTIONS"]
)
@require_permission("connectors", "write")
def github_app_claim_installation(user_id, installation_id):
    """Link an existing App installation to the current Aurora user.

    Counterpart to ``/app/discover-installations`` — the user picks one
    from the discovery list and explicitly asserts ownership. We verify
    the installation exists via ``/app/installations/{id}`` before
    INSERTing so a guess at a random installation_id can't succeed.

    Single-tenant deployments are safe by construction (one Aurora user
    == one operator). Multi-tenant deployments accept that the user
    asserts ownership; the audit log line below makes the claim
    traceable.
    """
    if not flask.current_app.config.get("GITHUB_APP_ENABLED"):
        return jsonify({"error": "GitHub App not configured"}), 503

    try:
        app_jwt = mint_app_jwt()
    except GitHubAppJWTError:
        logger.exception("[GITHUB-APP-CLAIM] JWT mint failed")
        return jsonify({"error": "GitHub App not configured"}), 503

    try:
        resp = requests.get(
            f"https://api.github.com/app/installations/{installation_id}",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
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

    account = data.get("account") if isinstance(data, dict) else None
    account_login = (account or {}).get("login")
    account_id = (account or {}).get("id")
    account_type = (account or {}).get("type")
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
    return jsonify({"success": True, "installation_id": installation_id})


@github_app_bp.route(
    "/app/installations/<int:installation_id>", methods=["DELETE", "OPTIONS"]
)
@require_permission("connectors", "write")
def github_app_unlink_installation(user_id, installation_id):
    """Remove the user->installation join row only.

    Does NOT uninstall the GitHub App from the user's GitHub account; that
    must be done by the user via GitHub's UI. Webhook handlers (Task 13)
    will reconcile if/when the user removes the install on GitHub's side.
    """
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """DELETE FROM user_github_installations
                        WHERE user_id = %s AND installation_id = %s""",
                    (user_id, installation_id),
                )
                deleted = cur.rowcount
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
    """Return the deployment's GitHub auth configuration.

    The frontend calls this once on dialog mount to decide which CTAs to
    render (Install GitHub App, Connect via OAuth, or both). Returning
    the mode server-side is mandatory because ``NEXT_PUBLIC_*`` vars
    cannot be trusted for security-relevant rendering and Aurora's
    client/backend boundary forbids client-derived identity.

    ``app_enabled`` reflects BOTH the configured mode AND whether the
    runtime actually validated the App env (``GITHUB_APP_ENABLED`` flag
    set by the boot-time validator). If mode is ``app``/``hybrid`` but
    required env is missing, every App route returns 503; reporting
    ``app_enabled=true`` here would render an Install CTA that
    immediately falls into a broken flow.
    """
    app_runtime_ready = bool(flask.current_app.config.get("GITHUB_APP_ENABLED"))
    return jsonify(
        {
            "mode": get_auth_mode(),
            "app_enabled": is_app_enabled() and app_runtime_ready,
            "oauth_enabled": is_oauth_enabled(),
            "oauth_configured": oauth_credentials_configured(),
        }
    )


@github_app_bp.route("/status", methods=["GET"])
@require_permission("connectors", "read")
def github_status(user_id):
    """Connection status for the GitHub connector.

    Hybrid-aware: a user is "connected" if they have either a non-suspended
    GitHub App installation linked OR (when OAuth is enabled) a stored
    user OAuth token. App identity wins when both are present, since the
    installation token has scoped permissions and survives user departure.

    Self-heals against silent uninstalls before reading from the DB so a
    revoked-on-GitHub install never registers as "connected" in the UI.
    """
    if is_app_enabled():
        _reconcile_user_installations(user_id)

    app_username: str | None = None
    if is_app_enabled():
        try:
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """SELECT gi.account_login
                             FROM user_github_installations ugi
                             JOIN github_installations gi
                                  ON gi.installation_id = ugi.installation_id
                            WHERE ugi.user_id = %s
                              AND ugi.disconnected_at IS NULL
                              AND gi.suspended_at IS NULL
                            ORDER BY ugi.is_primary DESC, ugi.linked_at DESC
                            LIMIT 1""",
                        (user_id,),
                    )
                    row = cur.fetchone()
                    if row:
                        app_username = row[0]
        except Exception as exc:
            logger.error(
                "[GITHUB-STATUS] DB read failed user=%s: %s",
                user_id, exc, exc_info=True,
            )
            return jsonify({"connected": False, "error": "Failed to check status"}), 500

    if app_username:
        return jsonify({"connected": True, "username": app_username, "auth_method": "app"})

    if is_oauth_enabled():
        try:
            creds = get_credentials_from_db(user_id, "github")
        except Exception as exc:
            logger.error(
                "[GITHUB-STATUS] OAuth credential read failed user=%s: %s",
                user_id, exc, exc_info=True,
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
    """Sever GitHub auth state for this user (Aurora side only).

    SOFT-deletes ``user_github_installations`` rows by setting
    ``disconnected_at = NOW()`` so the row survives. This matters
    because GitHub's install-flow callback typically does NOT re-fire
    when the App is already installed — without a soft-delete, a user
    who clicks Aurora's Disconnect and then Install GitHub App again
    would have nothing to relink to. Reconnect is just clearing
    ``disconnected_at`` (the install callback's UPSERT does this for us).

    Also removes any stored OAuth token. Does NOT uninstall the App on
    GitHub's side — the user must do that from their org settings if
    they want to fully revoke access.
    """
    soft_deleted_installs = 0
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE user_github_installations
                          SET disconnected_at = NOW()
                        WHERE user_id = %s
                          AND disconnected_at IS NULL""",
                    (user_id,),
                )
                soft_deleted_installs = cur.rowcount
                conn.commit()
    except Exception as exc:
        logger.error(
            "[GITHUB-DISCONNECT] DB soft-delete failed user=%s: %s",
            user_id, exc, exc_info=True,
        )
        return jsonify({"error": "Failed to disconnect"}), 500

    oauth_removed = False
    if is_oauth_enabled():
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
        "[GITHUB-DISCONNECT] user=%s soft_deleted_installs=%d oauth_removed=%s",
        user_id, soft_deleted_installs, oauth_removed,
    )
    return jsonify(
        {
            "success": True,
            "removed_installations": soft_deleted_installs,
            "oauth_token_removed": oauth_removed,
        }
    )
