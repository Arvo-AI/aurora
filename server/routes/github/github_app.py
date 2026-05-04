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

import flask
import requests
from flask import Blueprint, jsonify, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from utils.auth.github_app_jwt import GitHubAppJWTError, mint_app_jwt
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import validate_user_exists
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

github_app_bp = Blueprint("github_app", __name__)

FRONTEND_URL = os.getenv("FRONTEND_URL")
GITHUB_TIMEOUT = 20

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
                            (user_id, installation_id)
                       VALUES (%s, %s)
                       ON CONFLICT (user_id, installation_id) DO NOTHING""",
                    (user_id, installation_id),
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
    """List GitHub App installations linked to the requesting user."""
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
                        ORDER BY ugi.linked_at""",
                    (user_id,),
                )
                rows = cur.fetchall()
    except Exception as exc:
        logger.error(
            "[GITHUB-APP-LIST] DB read failed for user=%s: %s",
            user_id, exc, exc_info=True,
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
    except Exception as exc:
        logger.error(
            "[GITHUB-APP-UNLINK] DB delete failed user=%s installation_id=%d: %s",
            user_id, installation_id, exc, exc_info=True,
        )
        return jsonify({"error": "Failed to unlink installation"}), 500

    if deleted == 0:
        return jsonify({"error": "Installation link not found"}), 404

    logger.info(
        "[GITHUB-APP-UNLINK] removed user=%s installation_id=%d",
        user_id, installation_id,
    )
    return jsonify({"success": True, "installation_id": installation_id})
