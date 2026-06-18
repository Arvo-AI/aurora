"""GitHub OAuth routes — gated by ``GITHUB_AUTH_MODE``.

This module is the inverse of ``github_app.py``: it serves the legacy
user-token OAuth flow that on-prem deployments may prefer when they
cannot host their own GitHub App. Both modes can coexist (``hybrid``),
in which case the dialog shows both CTAs.

Routes:
    POST /github/login    — initiate OAuth (returns ``oauth_url``)
    GET  /github/callback — exchange code, store user token

When ``GITHUB_AUTH_MODE=app`` (the default), ``/github/login`` returns 404
so a misconfigured client cannot start an OAuth flow. ``/github/callback``
is always registered (it's an external endpoint hit by GitHub) but
returns a templated error page when OAuth is disabled, since by the time
GitHub redirects there a user has already taken the App path.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urlencode

import flask
import requests
from flask import Blueprint, jsonify, request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from utils.auth.github_auth_mode import (
    is_oauth_login_enabled,
    oauth_credentials_configured,
)
from utils.auth.rbac_decorators import require_permission
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

github_oauth_bp = Blueprint("github_oauth", __name__)

FRONTEND_URL = os.getenv("FRONTEND_URL", "")
GITHUB_TIMEOUT = 20
_OAUTH_STATE_SALT = "aurora.github.oauth.state.v1"
_OAUTH_STATE_TTL_SEC = 10 * 60
_CALLBACK_ERROR_TEMPLATE = "github_callback_error.html"


def _state_serializer() -> URLSafeTimedSerializer:
    secret = os.getenv("FLASK_SECRET_KEY") or flask.current_app.secret_key
    if not secret:
        raise RuntimeError("FLASK_SECRET_KEY is required to sign OAuth state")
    return URLSafeTimedSerializer(secret, salt=_OAUTH_STATE_SALT)


def _sign_state(user_id: str) -> str:
    return _state_serializer().dumps({"user_id": user_id})


def _verify_state(state: str) -> str | None:
    try:
        payload = _state_serializer().loads(state, max_age=_OAUTH_STATE_TTL_SEC)
    except BadSignature:
        return None
    if not isinstance(payload, dict):
        return None
    user_id = payload.get("user_id")
    return user_id if isinstance(user_id, str) and user_id else None


def _oauth_disabled_response():
    """Standard 404 body when OAuth mode is off."""
    return (
        jsonify(
            {
                "error": "OAuth is disabled in this deployment",
                "error_code": "GITHUB_OAUTH_DISABLED",
                "message": (
                    "GitHub OAuth is not enabled. Set "
                    "GITHUB_AUTH_MODE=oauth or =hybrid to enable it."
                ),
            }
        ),
        404,
    )


@github_oauth_bp.route("/login", methods=["POST"])
@require_permission("connectors", "write")
def github_login(user_id):
    """Initiate the GitHub OAuth flow.

    Returns ``{oauth_url}`` for the frontend to open in a popup. The user
    is redirected back to ``/github/callback`` after consenting on GitHub.
    """
    if not is_oauth_login_enabled():
        return _oauth_disabled_response()

    if not oauth_credentials_configured():
        logger.error(
            "[GITHUB-OAUTH] Login attempted with unconfigured client credentials"
        )
        return (
            jsonify(
                {
                    "error": "GitHub OAuth is not configured",
                    "error_code": "GITHUB_NOT_CONFIGURED",
                    "message": (
                        "GH_OAUTH_CLIENT_ID and GH_OAUTH_CLIENT_SECRET must be "
                        "set in the server environment to enable OAuth."
                    ),
                }
            ),
            400,
        )

    client_id = os.getenv("GH_OAUTH_CLIENT_ID", "")

    backend_url = (os.getenv("NEXT_PUBLIC_BACKEND_URL", "") or "").rstrip("/")
    redirect_uri = (
        f"{backend_url}/github/callback"
        if backend_url
        else f"{request.host_url.rstrip('/')}/github/callback"
    )

    try:
        signed_state = _sign_state(user_id)
    except RuntimeError:
        logger.error("[GITHUB-OAUTH] FLASK_SECRET_KEY missing; cannot sign state")
        return (
            jsonify(
                {
                    "error": "Server is not configured to sign OAuth state",
                    "error_code": "GITHUB_OAUTH_STATE_UNCONFIGURED",
                }
            ),
            500,
        )

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "repo,user",
        "state": signed_state,
    }
    oauth_url = f"https://github.com/login/oauth/authorize?{urlencode(params)}"

    return jsonify(
        {
            "oauth_url": oauth_url,
            "message": "Redirect to GitHub for authentication",
        }
    )


@github_oauth_bp.route("/callback", methods=["GET"])
def github_callback():
    """Exchange the OAuth code for a user access token and store it.

    Always registered (GitHub redirects here directly and a session cookie
    is not guaranteed). Returns a templated success/error page that posts
    a message to the popup opener.
    """
    if not is_oauth_login_enabled():
        return flask.render_template(
            _CALLBACK_ERROR_TEMPLATE,
            error="GitHub OAuth is disabled in this deployment",
            frontend_url=FRONTEND_URL,
        )

    raw_state = request.args.get("state")
    if not raw_state:
        logger.error("[GITHUB-OAUTH] callback missing state")
        return flask.render_template(
            _CALLBACK_ERROR_TEMPLATE,
            error="Missing state parameter",
            frontend_url=FRONTEND_URL,
        )

    aurora_user_id = _verify_state(raw_state)
    if not aurora_user_id:
        logger.error("[GITHUB-OAUTH] callback state failed signature/expiry check")
        return flask.render_template(
            _CALLBACK_ERROR_TEMPLATE,
            error="Invalid or expired state parameter",
            frontend_url=FRONTEND_URL,
        )

    code = request.args.get("code")
    if not code:
        logger.error("[GITHUB-OAUTH] callback hit with no code")
        return flask.render_template(
            _CALLBACK_ERROR_TEMPLATE,
            error="No authorization code provided",
            frontend_url=FRONTEND_URL,
        )

    if not oauth_credentials_configured():
        logger.error("[GITHUB-OAUTH] callback hit with unconfigured client credentials")
        return flask.render_template(
            _CALLBACK_ERROR_TEMPLATE,
            error="GitHub integration is not properly configured",
            frontend_url=FRONTEND_URL,
        )

    client_id = os.getenv("GH_OAUTH_CLIENT_ID", "")
    client_secret = os.getenv("GH_OAUTH_CLIENT_SECRET", "")

    try:
        token_response = requests.post(
            "https://github.com/login/oauth/access_token",
            json={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
            },
            headers={"Accept": "application/json"},
            timeout=GITHUB_TIMEOUT,
        )
    except requests.RequestException:
        logger.exception("[GITHUB-OAUTH] token exchange request failed")
        return flask.render_template(
            _CALLBACK_ERROR_TEMPLATE,
            error="Failed to reach GitHub during token exchange",
            frontend_url=FRONTEND_URL,
        )

    if token_response.status_code != 200:
        logger.error(
            "[GITHUB-OAUTH] token exchange returned non-200: status=%s",
            token_response.status_code,
        )
        return flask.render_template(
            _CALLBACK_ERROR_TEMPLATE,
            error="Failed to authenticate with GitHub",
            frontend_url=FRONTEND_URL,
        )

    try:
        token_data = token_response.json()
    except ValueError:
        logger.exception("[GITHUB-OAUTH] token response body was not JSON")
        return flask.render_template(
            _CALLBACK_ERROR_TEMPLATE,
            error="Invalid response from GitHub",
            frontend_url=FRONTEND_URL,
        )
    access_token = token_data.get("access_token") if isinstance(token_data, dict) else None
    if not access_token:
        logger.error("[GITHUB-OAUTH] no access_token in token response")
        return flask.render_template(
            _CALLBACK_ERROR_TEMPLATE,
            error="Invalid response from GitHub",
            frontend_url=FRONTEND_URL,
        )

    try:
        user_response = requests.get(
            "https://api.github.com/user",
            headers={"Authorization": f"token {access_token}"},
            timeout=GITHUB_TIMEOUT,
        )
    except requests.RequestException:
        logger.exception("[GITHUB-OAUTH] user info request failed")
        return flask.render_template(
            _CALLBACK_ERROR_TEMPLATE,
            error="Failed to fetch user information from GitHub",
            frontend_url=FRONTEND_URL,
        )

    if user_response.status_code != 200:
        logger.error(
            "[GITHUB-OAUTH] user info returned non-200: status=%s",
            user_response.status_code,
        )
        return flask.render_template(
            _CALLBACK_ERROR_TEMPLATE,
            error="Failed to fetch user information",
            frontend_url=FRONTEND_URL,
        )

    try:
        user_data = user_response.json()
    except ValueError:
        logger.exception("[GITHUB-OAUTH] user info response body was not JSON")
        return flask.render_template(
            _CALLBACK_ERROR_TEMPLATE,
            error="Invalid response from GitHub",
            frontend_url=FRONTEND_URL,
        )
    if not isinstance(user_data, dict):
        logger.error("[GITHUB-OAUTH] user info response not a JSON object")
        return flask.render_template(
            _CALLBACK_ERROR_TEMPLATE,
            error="Invalid response from GitHub",
            frontend_url=FRONTEND_URL,
        )
    github_username = user_data.get("login")
    github_user_id = user_data.get("id")

    try:
        from utils.auth.token_management import store_tokens_in_db

        store_tokens_in_db(
            aurora_user_id,
            {
                "access_token": access_token,
                "username": github_username,
                "user_id": github_user_id,
                "api_url": "https://api.github.com",
            },
            "github",
        )
    except Exception:
        safe_user = sanitize(aurora_user_id).replace("\r", "_").replace("\n", "_")
        logger.exception(
            "[GITHUB-OAUTH] failed to persist token for user=%s", safe_user
        )
        return flask.render_template(
            _CALLBACK_ERROR_TEMPLATE,
            error="Failed to persist credentials",
            frontend_url=FRONTEND_URL,
        )

    try:
        from chat.backend.agent.tools.mcp_tools import clear_credentials_cache

        clear_credentials_cache(aurora_user_id)
    except Exception as cache_err:
        safe_user = sanitize(aurora_user_id).replace("\r", "_").replace("\n", "_")
        logger.warning(
            "[GITHUB-OAUTH] failed to clear MCP cache for user=%s: %s",
            safe_user,
            cache_err,
        )

    try:
        from utils.auth.tool_registry import seed_org_tool_permissions
        from utils.auth.stateless_auth import get_org_id_for_user
        org_id = get_org_id_for_user(aurora_user_id)
        if org_id:
            seed_org_tool_permissions(org_id, aurora_user_id)
    except Exception:
        logger.warning("[GITHUB-OAUTH] failed to seed tool permissions", exc_info=True)

    return flask.render_template(
        "github_callback_success.html",
        token="",
        github_username=github_username,
        frontend_url=FRONTEND_URL,
    )
