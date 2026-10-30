"""One-click "Add to Slack" signup flow (hosted deployments only).

Lets a visitor on the marketing website install Aurora's Slack agent AND get
an Aurora account in a single Slack screen:

    GET /slack/signup/start -> 302 to Slack's OAuth v2 install page
    GET /slack/callback     -> (signup-salted state) provisions org+user,
                               connects the workspace (incidents channel,
                               Slack memory, channel auto-registration), then
                               redirects to the app with a one-time handoff
                               token that logs the browser in.

The callback is the SAME redirect URL the in-app connect flow uses, so the
Slack app needs no extra configuration. ``slack_routes.slack_callback``
dispatches here when the state verifies under the signup salt; the in-app
flow's state is a bare user id and can never verify as a signed token.

Gated on ``HOSTED_SIGNUP_ENABLED`` (shared with the GitHub one-click flow)
plus Slack OAuth credentials. Self-hosted/OSS deployments keep invite-only
registration.

Invariants (mirror ``routes.github.github_signup`` — do NOT relax):

    1. ``state`` MUST verify under the signup salt (signed + expiring).
    2. The installer's identity comes ONLY from exchanging the OAuth ``code``
       with Slack (``authed_user`` + ``team``) and ``users.info`` read with
       the freshly issued bot token — never from query params.
    3. Error templates render HARD-CODED strings only.
    4. An existing Aurora account is auto-logged-in ONLY when it already owns
       this exact Slack identity (same workspace AND same Slack user, linked
       on the user row or stored on its connection). An email-only match
       redirects to sign-in.
    5. A workspace already connected to an Aurora org is never connected to
       a second one: Slack events route by workspace, so a duplicate would
       split or hijack another org's traffic. The installer joins that org
       instead, under the "Sign in with Slack" rules.
"""

from __future__ import annotations

import logging
import os
import secrets

import bcrypt
import flask
import requests
from flask import Blueprint, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from connectors.slack_connector.oauth import CLIENT_ID, CLIENT_SECRET, get_auth_url
from routes.github.github_signup import (
    _attribution_from,
    _unique_org_identity,
    _with_attribution,
    is_hosted_signup_enabled,
)
from routes.slack.slack_signin import (
    bind_to_browser,
    find_user_by_slack_identity,
    forget_browser,
    handoff_redirect,
    join_workspace_org,
    sign_in_error,
    start_on_callback_host,
    started_in_this_browser,
)
from utils.auth import normalize_email
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

slack_signup_bp = Blueprint("slack_signup", __name__)

FRONTEND_URL = os.getenv("FRONTEND_URL") or ""
# Marketing site the "Add to Slack" button lives on; a cancelled install goes
# back there. Unset (self-hosted) falls back to the app's sign-in page.
MARKETING_SITE_URL = (os.getenv("MARKETING_SITE_URL") or "").rstrip("/")
SLACK_TIMEOUT = 20

_SIGNUP_STATE_TTL_SEC = 30 * 60
_SIGNUP_STATE_SALT = "aurora.slack.signup-state.v1"
SIGNUP_COOKIE = "aurora_slack_signup"

# Hard-coded user-facing error strings. NEVER substitute query params.
_ERROR_NOT_AVAILABLE = "One-click signup is not available on this deployment"
_ERROR_MISSING_PARAMS = "Missing required parameters from Slack callback"
_ERROR_TOKEN_EXCHANGE = "Could not complete the Slack installation. Please try again from the website."
_ERROR_IDENTITY = "Could not verify your Slack identity"
_ERROR_EXPIRED = "This Slack install link is no longer valid. Start again from the website."
_ERROR_INTERNAL = "An internal error occurred while creating your account"


def _signup_ready() -> bool:
    return is_hosted_signup_enabled() and bool(CLIENT_ID) and bool(CLIENT_SECRET)


def _state_serializer() -> URLSafeTimedSerializer:
    secret = os.getenv("FLASK_SECRET_KEY") or flask.current_app.secret_key
    if not secret:
        raise RuntimeError("FLASK_SECRET_KEY is not configured; cannot sign signup state")
    return URLSafeTimedSerializer(secret, salt=_SIGNUP_STATE_SALT)


def _sign_signup_state(attribution: dict | None = None) -> tuple[str, str]:
    """Signed state plus its nonce, which the start route also puts in a cookie."""
    nonce = secrets.token_urlsafe(16)
    payload = {"flow": "slack_signup", "nonce": nonce}
    if attribution:
        payload["attr"] = attribution
    return _state_serializer().dumps(payload), nonce


def load_signup_state(state: str) -> dict | None:
    """Payload of a valid, unexpired signup state; None for anything else
    (including the in-app flow's bare user-id state)."""
    try:
        payload = _state_serializer().loads(state, max_age=_SIGNUP_STATE_TTL_SEC)
    except SignatureExpired:
        logger.warning("[SLACK-SIGNUP] state expired")
        return None
    except (BadSignature, RuntimeError):
        return None
    except Exception:
        logger.warning("[SLACK-SIGNUP] state could not be parsed")
        return None
    if isinstance(payload, dict) and payload.get("flow") == "slack_signup":
        return payload
    return None


def _render_error(reason: str) -> flask.Response:
    return flask.make_response(
        flask.render_template(
            "github_callback_error.html",
            error=reason,
            provider="Slack",
            frontend_url=FRONTEND_URL,
        )
    )


@slack_signup_bp.route("/signup/start", methods=["GET"])
def slack_signup_start():
    """Unauthenticated entry point for the website's "Add to Slack" CTA."""
    if not _signup_ready():
        return _render_error(_ERROR_NOT_AVAILABLE)
    hop = start_on_callback_host()
    if hop is not None:
        return hop
    try:
        state, nonce = _sign_signup_state(_attribution_from(request.args))
    except RuntimeError:
        logger.exception("[SLACK-SIGNUP] failed to sign signup state")
        return _render_error(_ERROR_INTERNAL)
    return bind_to_browser(flask.redirect(get_auth_url(state), code=302),
                           SIGNUP_COOKIE, nonce, _SIGNUP_STATE_TTL_SEC)


_UNCONFIRMED = {"unconfirmed": True}


def _fetch_installer_identity(token_data: dict) -> dict | None:
    """Installer identity from the token exchange, enriched via ``users.info``.

    Returns ``{"slack_user_id", "team_id", "team_name", "email", "name"}``,
    ``_UNCONFIRMED`` when Slack hasn't confirmed the email, or None. The bot token was minted for this exact install seconds ago, so
    ``users.info`` reads the profile Slack itself holds for ``authed_user``.
    """
    access_token = token_data.get("access_token")
    authed_user = token_data.get("authed_user") or {}
    team = token_data.get("team") or {}
    slack_user_id = authed_user.get("id") if isinstance(authed_user, dict) else None
    team_id = team.get("id") if isinstance(team, dict) else None
    if not (isinstance(access_token, str) and isinstance(slack_user_id, str)
            and isinstance(team_id, str) and slack_user_id and team_id):
        return None

    try:
        resp = requests.get(
            "https://slack.com/api/users.info",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"user": slack_user_id},
            timeout=SLACK_TIMEOUT,
        )
        data = resp.json() if resp.status_code == 200 else None
    except (requests.RequestException, ValueError):
        logger.exception("[SLACK-SIGNUP] users.info request failed")
        return None
    if not isinstance(data, dict) or not data.get("ok"):
        logger.warning("[SLACK-SIGNUP] users.info failed: %s",
                       (data or {}).get("error") if isinstance(data, dict) else "bad response")
        return None

    user = data.get("user")
    if not isinstance(user, dict) or user.get("is_bot") or user.get("deleted"):
        return None
    if user.get("team_id") != team_id:
        return None
    # The address becomes a verified Aurora login, so Slack must have
    # confirmed it. A profile email alone can be set by a workspace admin.
    if user.get("is_email_confirmed") is not True:
        logger.info("[SLACK-SIGNUP] installer's Slack email is not confirmed")
        return _UNCONFIRMED
    profile = user.get("profile")
    email = profile.get("email") if isinstance(profile, dict) else None
    if not isinstance(email, str) or "@" not in email:
        return None

    name = profile.get("real_name") or user.get("real_name") or user.get("name")
    return {
        "slack_user_id": slack_user_id,
        "team_id": team_id,
        "team_name": team.get("name") if isinstance(team.get("name"), str) else "Slack",
        "email": normalize_email(email),
        "name": name if isinstance(name, str) and name else email.split("@", 1)[0],
    }


def _provision_user(identity: dict):
    """Create the Aurora user + org. Returns ``(user_id, org_id)`` or an
    error/redirect response."""
    conn = db_pool.get_admin_connection()
    try:
        with conn as held_conn:
            with held_conn.cursor() as cur:
                cur.execute("SELECT 1 FROM users WHERE LOWER(email) = %s", (identity["email"],))
                if cur.fetchone():
                    # Invariant #4: email alone never logs anyone in. The
                    # sign-in page resumes the Slack connect after login.
                    logger.info("[SLACK-SIGNUP] email collision with existing account; redirecting to sign-in")
                    return sign_in_error("slack_account_exists")

                # Slack-provisioned users log in through Slack (handoff now,
                # "Sign in with Slack" later), so the unusable password must
                # not trap them on the forced change-password screen.
                unusable = bcrypt.hashpw(os.urandom(32), bcrypt.gensalt())
                cur.execute(
                    """INSERT INTO users (email, password_hash, name, role,
                                          email_verified, must_change_password,
                                          slack_team_id, slack_user_id, created_at)
                       VALUES (%s, %s, %s, 'admin', TRUE, FALSE, %s, %s, NOW())
                       RETURNING id""",
                    (identity["email"], unusable.decode("utf-8"), identity["name"],
                     identity["team_id"], identity["slack_user_id"]),
                )
                user_id = cur.fetchone()[0]
                org_name, slug = _unique_org_identity(cur, identity["team_name"])
                cur.execute(
                    """INSERT INTO organizations (id, name, slug, created_by)
                       VALUES (gen_random_uuid()::TEXT, %s, %s, %s)
                       RETURNING id""",
                    (org_name, slug, user_id),
                )
                org_id = cur.fetchone()[0]
                cur.execute("UPDATE users SET org_id = %s WHERE id = %s", (org_id, user_id))
                held_conn.commit()
    except Exception:
        logger.exception("[SLACK-SIGNUP] provisioning transaction failed")
        return _render_error(_ERROR_INTERNAL)

    # Same post-registration seeding as /api/auth/register — all best-effort.
    try:
        from utils.auth.enforcer import assign_role_to_user
        assign_role_to_user(user_id, "admin", org_id)
    except Exception:
        logger.warning("[SLACK-SIGNUP] Casbin role assignment failed", exc_info=True)
    try:
        from utils.auth.command_policy import seed_default_command_policy
        seed_default_command_policy(org_id, user_id)
    except Exception:
        logger.warning("[SLACK-SIGNUP] command policy seeding failed", exc_info=True)
    try:
        from utils.auth.tool_registry import seed_org_tool_permissions
        seed_org_tool_permissions(org_id, user_id)
    except Exception:
        logger.warning("[SLACK-SIGNUP] tool permission seeding failed", exc_info=True)
    try:
        from routes.audit_routes import record_audit_event
        record_audit_event(org_id, user_id, "register", "organization", org_id,
                           {"via": "slack_one_click"}, request)
    except Exception:
        logger.warning("[SLACK-SIGNUP] audit event failed", exc_info=True)

    from utils.hooks import get_hook
    get_hook("after_user_created")(user_id, org_id, identity["email"], "slack", True)
    return user_id, org_id


def _cancelled_redirect() -> flask.Response:
    """The visitor clicked Cancel on Slack's screen: back to where they were."""
    if MARKETING_SITE_URL:
        return flask.redirect(f"{MARKETING_SITE_URL}/?slack=cancelled#add-to-slack", code=302)
    return sign_in_error("slack_cancelled")


def handle_signup_callback(code: str | None, state_payload: dict, slack_error: str | None = None) -> flask.Response:
    """Signup branch of ``/slack/callback``; the state is already verified."""
    return forget_browser(_signup_callback(code, state_payload, slack_error), SIGNUP_COOKIE)


def _signup_callback(code: str | None, state_payload: dict, slack_error: str | None) -> flask.Response:
    from routes.slack.slack_events_helpers import get_user_id_from_slack_team
    from routes.slack.slack_routes import complete_slack_connection
    from connectors.slack_connector.oauth import exchange_code_for_token

    attr = state_payload.get("attr")
    attribution = _attribution_from(attr) if isinstance(attr, dict) else {}

    if not _signup_ready():
        return _render_error(_ERROR_NOT_AVAILABLE)
    if slack_error == "access_denied":
        return _cancelled_redirect()
    if not code:
        return _render_error(_ERROR_MISSING_PARAMS)
    # Before the code is exchanged, so a forwarded link can't spend it.
    if not started_in_this_browser(SIGNUP_COOKIE, state_payload.get("nonce", "")):
        logger.warning("[SLACK-SIGNUP] state was not started in this browser")
        return _render_error(_ERROR_EXPIRED)

    try:
        token_data = exchange_code_for_token(code)
    except Exception:
        logger.warning("[SLACK-SIGNUP] token exchange failed", exc_info=True)
        return _render_error(_ERROR_TOKEN_EXCHANGE)

    identity = _fetch_installer_identity(token_data)
    if identity is _UNCONFIRMED:
        return sign_in_error("slack_email_unconfirmed")
    if identity is None:
        return _render_error(_ERROR_IDENTITY)

    # Invariant #4: a returning installer is recognized by the Slack
    # identity linked to their Aurora account.
    user_id = find_user_by_slack_identity(identity["team_id"], identity["slack_user_id"])
    if user_id and get_user_id_from_slack_team(identity["team_id"]):
        # Already set up: re-running the connection would post a second
        # welcome. Just sign them in.
        logger.info("[SLACK-SIGNUP] returning installer signed in user=%s", user_id)
        return _with_attribution(handoff_redirect(user_id, via="slack", slack_auth="already"), attribution)

    created = False
    if user_id is None:
        # Invariant #5: the workspace belongs to an Aurora org already, so
        # this person joins it rather than creating a second org for it.
        joined = join_workspace_org(identity)
        if joined is not None:
            if not isinstance(joined, str):
                return _with_attribution(joined, attribution)
            logger.info("[SLACK-SIGNUP] workspace already connected; installer joined its org")
            return _with_attribution(handoff_redirect(joined, via="slack_join"), attribution)

        provisioned = _provision_user(identity)
        if not isinstance(provisioned, tuple):
            return _with_attribution(provisioned, attribution)
        user_id, _org_id = provisioned
        created = True

    # A new account still gets logged in if Slack setup fails, so the user
    # lands in the app (where the onboarding page can retry) instead of a
    # dead end. A returning installer whose workspace was disconnected gets
    # reconnected here.
    connect_error = complete_slack_connection(user_id, token_data)
    if connect_error:
        logger.warning("[SLACK-SIGNUP] connection setup failed user=%s error=%s", user_id, connect_error)
    logger.info("[SLACK-SIGNUP] signed in user=%s team=%s created=%s connected=%s",
                user_id, identity["team_id"], created, not connect_error)
    slack_auth = "failed" if connect_error else "success"
    return _with_attribution(handoff_redirect(user_id, via="slack", slack_auth=slack_auth), attribution)
