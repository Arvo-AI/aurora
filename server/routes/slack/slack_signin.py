"""Sign in with Slack (OpenID Connect), hosted deployments only.

    GET /slack/signin/available -> {"available": bool} for the sign-in page
    GET /slack/signin/start     -> 302 to Slack's OpenID authorize page
    GET /slack/callback         -> (signin-salted state) resolves the Slack
                                   identity to an Aurora account and redirects
                                   to the app with a one-time handoff token.

Like the "Add to Slack" signup, the callback is the existing ``/slack/callback``
redirect URL, dispatched by state salt, so the Slack app needs no new config.

Both flows bind their state to the browser that started them: the start route
sets a nonce cookie, and the callback refuses a state whose nonce the browser
doesn't hold, before the Slack code is spent. Otherwise a forwarded callback
link could log someone into the sender's account (login CSRF).

Who gets in:

    1. An account already linked to this exact Slack identity (team + user),
       or the person who installed Aurora in the workspace.
    2. A full member of a workspace that is connected to an Aurora org joins
       that org as a viewer. Workspace membership is checked with the org's
       bot token, so guests and Slack Connect users are refused.
    3. Anyone else is sent to the "Add to Slack" flow instead.

An existing account found only by email is linked on first Slack sign-in when
it is in the same org and is not an admin. Admins sign in with their password
or SSO: a Slack workspace admin can change members' emails, and that must not
be a path to an Aurora admin account.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
from urllib.parse import urlencode, urlparse

import bcrypt
import flask
import requests
from flask import Blueprint, jsonify, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from connectors.slack_connector.oauth import CLIENT_ID, CLIENT_SECRET, REDIRECT_URI
from routes.github.github_signup import is_hosted_signup_enabled
from utils.auth import normalize_email
from utils.auth.handoff import mint_handoff
from utils.db.connection_pool import db_pool
from utils.web.limiter_ext import limiter
from utils.web.public_url import external_backend_url

logger = logging.getLogger(__name__)

slack_signin_bp = Blueprint("slack_signin", __name__)

FRONTEND_URL = os.getenv("FRONTEND_URL") or ""
SLACK_TIMEOUT = 20

_SIGNIN_STATE_TTL_SEC = 15 * 60
_SIGNIN_STATE_SALT = "aurora.slack.signin-state.v1"
_OIDC_AUTHORIZE_URL = "https://slack.com/openid/connect/authorize"
_OIDC_TOKEN_URL = "https://slack.com/api/openid.connect.token"
_OIDC_USERINFO_URL = "https://slack.com/api/openid.connect.userInfo"

JOINED_ROLE = "viewer"

SIGNIN_COOKIE = "aurora_slack_signin"
_CALLBACK_PATH = "/slack/callback"


def signin_ready() -> bool:
    return is_hosted_signup_enabled() and bool(CLIENT_ID) and bool(CLIENT_SECRET)


def _state_serializer() -> URLSafeTimedSerializer:
    secret = os.getenv("FLASK_SECRET_KEY") or flask.current_app.secret_key
    if not secret:
        raise RuntimeError("FLASK_SECRET_KEY is not configured; cannot sign sign-in state")
    return URLSafeTimedSerializer(secret, salt=_SIGNIN_STATE_SALT)


def load_signin_state(state: str) -> dict | None:
    """Payload of a valid, unexpired sign-in state; None for anything else."""
    try:
        payload = _state_serializer().loads(state, max_age=_SIGNIN_STATE_TTL_SEC)
    except SignatureExpired:
        logger.warning("[SLACK-SIGNIN] state expired")
        return None
    except (BadSignature, RuntimeError):
        return None
    except Exception:
        logger.warning("[SLACK-SIGNIN] state could not be parsed")
        return None
    if isinstance(payload, dict) and payload.get("flow") == "slack_signin" and payload.get("nonce"):
        return payload
    return None


def sign_in_error(code: str) -> flask.Response:
    """Redirect to the sign-in page with a fixed error code it knows how to word."""
    return flask.redirect(f"{FRONTEND_URL}/sign-in?error={code}", code=302)


def start_on_callback_host(path: str, params: dict | None = None) -> flask.Response | None:
    """Redirect a start request to the host Slack will call back.

    The browser-binding cookie only comes back to the host that set it. In
    local dev the button points at localhost while Slack returns to the
    tunnel, so hop once (``hop=1`` stops a loop if a proxy rewrites Host).
    The host is our own configured URL and only ``params`` the caller has
    already allow-listed travel with it.
    """
    callback = urlparse(external_backend_url())
    if not callback.netloc or request.host == callback.netloc or request.args.get("hop"):
        return None
    query = urlencode({**(params or {}), "hop": "1"})
    return flask.redirect(f"{external_backend_url()}{path}?{query}", code=302)


def bind_to_browser(response: flask.Response, cookie: str, nonce: str, max_age: int) -> flask.Response:
    """Remember which browser started this flow; only the callback reads it."""
    response.set_cookie(
        cookie, nonce, max_age=max_age, path=_CALLBACK_PATH, httponly=True,
        secure=external_backend_url().startswith("https://"), samesite="Lax",
    )
    return response


def started_in_this_browser(cookie: str, nonce: str) -> bool:
    return secrets.compare_digest(request.cookies.get(cookie, ""), nonce or "")


def forget_browser(response: flask.Response, cookie: str) -> flask.Response:
    response.delete_cookie(cookie, path=_CALLBACK_PATH)
    return response


def handoff_redirect(user_id: str, via: str, slack_auth: str | None = None) -> flask.Response:
    """Log the browser in with a one-time token and send it to the app.

    Every Slack path ends here, so this is where an org that requires SSO
    keeps Slack from becoming a way around it, and where an account whose
    role assignment failed earlier gets it before it can sign in.
    """
    from utils.auth.saml_sso import org_requires_sso

    conn = db_pool.get_admin_connection()
    try:
        with conn as held_conn:
            with held_conn.cursor() as cur:
                cur.execute("SELECT org_id, email, role FROM users WHERE id = %s", (user_id,))
                row = cur.fetchone()
                if not row:
                    return sign_in_error("slack_internal")
                org_id, email, role = row
                if org_requires_sso(cur, org_id, email):
                    return sign_in_error("slack_sso_required")
                if not ensure_org_role(user_id, org_id, role):
                    return sign_in_error("slack_internal")
                token = mint_handoff(cur, user_id)
                held_conn.commit()
    except Exception:
        logger.exception("[SLACK] handoff mint failed")
        return sign_in_error("handoff_failed")
    params = {"handoff": token, "via": via}
    if slack_auth:
        params["slack_auth"] = slack_auth
    return flask.redirect(f"{FRONTEND_URL}/sign-in?{urlencode(params)}", code=302)


def ensure_org_role(user_id: str, org_id: str | None, role: str | None) -> bool:
    """Give the user their stored role in the org if they have none yet.

    Signup and join assign the role best-effort after the user row commits;
    without one, every RBAC route denies the session. Never replaces an
    existing role, so a promotion made in the app is kept.
    """
    if not org_id:
        return False
    try:
        from utils.auth.enforcer import assign_role_to_user, get_enforcer, reload_if_stale
        reload_if_stale()
        if get_enforcer().get_roles_for_user_in_domain(user_id, org_id):
            return True
        assign_role_to_user(user_id, role or JOINED_ROLE, org_id)
        return True
    except Exception:
        logger.exception("[SLACK] could not give user %s a role in their org", user_id)
        return False


def find_user_by_slack_identity(team_id: str, slack_user_id: str) -> str | None:
    """Aurora account linked to this Slack identity, else the workspace installer."""
    try:
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM users WHERE slack_team_id = %s AND slack_user_id = %s",
                (team_id, slack_user_id),
            )
            row = cur.fetchone()
    except Exception:
        logger.exception("[SLACK] identity lookup failed")
        row = None
    if row:
        return row[0]
    from routes.slack.slack_events_helpers import get_user_id_from_slack_user
    return get_user_id_from_slack_user(slack_user_id, team_id)


def join_workspace_org(identity: dict) -> str | flask.Response | None:
    """Sign a member of an already-connected workspace into that workspace's org.

    Returns the Aurora user id, a redirect explaining why not, or None when
    the workspace is not connected to any org (the caller decides what then).
    """
    from routes.slack.slack_events_helpers import get_user_id_from_slack_team, get_workspace_member
    from connectors.slack_connector.client import get_slack_client_for_user
    from utils.auth.saml_sso import org_requires_sso
    from utils.auth.stateless_auth import get_org_id_for_user

    workspace_user_id = get_user_id_from_slack_team(identity["team_id"])
    if not workspace_user_id:
        return None
    org_id = get_org_id_for_user(workspace_user_id)
    client = get_slack_client_for_user(workspace_user_id)
    if not org_id or not client:
        logger.warning("[SLACK-SIGNIN] connected workspace has no usable org or bot token")
        return sign_in_error("slack_internal")

    # The org's own bot token says whether this is a full member, so a guest
    # or a Slack Connect user from another company never joins the org.
    if get_workspace_member(client, identity["slack_user_id"], identity["team_id"]) is None:
        return sign_in_error("slack_not_member")

    created = False
    try:
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            if org_requires_sso(cur, org_id, identity["email"]):
                return sign_in_error("slack_sso_required")
            cur.execute(
                "SELECT id, org_id, role, slack_user_id FROM users WHERE LOWER(email) = %s",
                (identity["email"],),
            )
            existing = cur.fetchone()
            if existing:
                user_id, user_org_id, role, linked_slack_user = existing
                if user_org_id != org_id:
                    return sign_in_error("account_exists")
                if linked_slack_user and linked_slack_user != identity["slack_user_id"]:
                    return sign_in_error("slack_identity_mismatch")
                if role == "admin":
                    return sign_in_error("slack_admin_password")
                cur.execute(
                    """UPDATE users SET slack_team_id = %s, slack_user_id = %s,
                                        email_verified = TRUE, must_change_password = FALSE
                        WHERE id = %s""",
                    (identity["team_id"], identity["slack_user_id"], user_id),
                )
            else:
                unusable = bcrypt.hashpw(os.urandom(32), bcrypt.gensalt()).decode("utf-8")
                cur.execute(
                    """INSERT INTO users (email, password_hash, name, role, org_id,
                                          email_verified, must_change_password,
                                          slack_team_id, slack_user_id, created_at)
                       VALUES (%s, %s, %s, %s, %s, TRUE, FALSE, %s, %s, NOW())
                       RETURNING id""",
                    (identity["email"], unusable, identity["name"], JOINED_ROLE, org_id,
                     identity["team_id"], identity["slack_user_id"]),
                )
                user_id = cur.fetchone()[0]
                created = True
            conn.commit()
    except Exception:
        logger.exception("[SLACK-SIGNIN] joining the workspace org failed")
        return sign_in_error("slack_internal")

    if created:
        try:
            from utils.auth.enforcer import assign_role_to_user
            assign_role_to_user(user_id, JOINED_ROLE, org_id)
        except Exception:
            logger.warning("[SLACK-SIGNIN] Casbin role assignment failed", exc_info=True)
        try:
            from routes.audit_routes import record_audit_event
            record_audit_event(org_id, user_id, "join", "organization", org_id,
                               {"via": "slack_workspace"}, request)
        except Exception:
            logger.warning("[SLACK-SIGNIN] audit event failed", exc_info=True)
    logger.info("[SLACK-SIGNIN] workspace member signed in user=%s created=%s", user_id, created)
    return user_id


@slack_signin_bp.route("/signin/available", methods=["GET"])
def slack_signin_available():
    return jsonify({"available": signin_ready()})


@slack_signin_bp.route("/signin/start", methods=["GET"])
@limiter.limit("10 per minute;30 per hour")
def slack_signin_start():
    """Unauthenticated entry point for the "Continue with Slack" button."""
    if not signin_ready():
        return sign_in_error("slack_unavailable")
    hop = start_on_callback_host("/slack/signin/start")
    if hop is not None:
        return hop
    nonce = secrets.token_urlsafe(16)
    try:
        state = _state_serializer().dumps({"flow": "slack_signin", "nonce": nonce})
    except RuntimeError:
        logger.exception("[SLACK-SIGNIN] failed to sign state")
        return sign_in_error("slack_internal")
    params = {
        "response_type": "code",
        "scope": "openid email profile",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "state": state,
        "nonce": nonce,
    }
    response = flask.redirect(f"{_OIDC_AUTHORIZE_URL}?{urlencode(params)}", code=302)
    return bind_to_browser(response, SIGNIN_COOKIE, nonce, _SIGNIN_STATE_TTL_SEC)


def _id_token_nonce(id_token: str) -> str | None:
    """``nonce`` claim of an id_token we just received from Slack over TLS."""
    try:
        payload = id_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError, TypeError):
        return None
    nonce = claims.get("nonce") if isinstance(claims, dict) else None
    return nonce if isinstance(nonce, str) else None


def _oidc_identity(code: str, expected_nonce: str) -> dict | None:
    """Exchange the code and read the signed-in Slack user. None on any failure."""
    try:
        token_resp = requests.post(
            _OIDC_TOKEN_URL,
            data={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
                  "code": code, "redirect_uri": REDIRECT_URI},
            timeout=SLACK_TIMEOUT,
        )
        tokens = token_resp.json()
    except (requests.RequestException, ValueError):
        logger.exception("[SLACK-SIGNIN] token exchange failed")
        return None
    if not isinstance(tokens, dict) or not tokens.get("ok"):
        logger.warning("[SLACK-SIGNIN] token exchange error: %s",
                       tokens.get("error") if isinstance(tokens, dict) else "bad response")
        return None
    access_token = tokens.get("access_token")
    id_token = tokens.get("id_token")
    if not isinstance(access_token, str) or not isinstance(id_token, str):
        return None
    if not secrets.compare_digest(_id_token_nonce(id_token) or "", expected_nonce):
        logger.warning("[SLACK-SIGNIN] id_token nonce mismatch")
        return None

    try:
        info_resp = requests.get(
            _OIDC_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=SLACK_TIMEOUT,
        )
        info = info_resp.json()
    except (requests.RequestException, ValueError):
        logger.exception("[SLACK-SIGNIN] userInfo failed")
        return None
    if not isinstance(info, dict) or not info.get("ok"):
        return None

    slack_user_id = info.get("https://slack.com/user_id") or info.get("sub")
    team_id = info.get("https://slack.com/team_id")
    email = info.get("email")
    if not (isinstance(slack_user_id, str) and isinstance(team_id, str)
            and isinstance(email, str) and "@" in email and info.get("email_verified") is True):
        return None
    name = info.get("name")
    team_name = info.get("https://slack.com/team_name")
    return {
        "slack_user_id": slack_user_id,
        "team_id": team_id,
        "team_name": team_name if isinstance(team_name, str) else "Slack",
        "email": normalize_email(email),
        "name": name if isinstance(name, str) and name else email.split("@", 1)[0],
    }


def handle_signin_callback(code: str | None, state_payload: dict) -> flask.Response:
    """Sign-in branch of ``/slack/callback``; the state is already verified."""
    return forget_browser(_signin_callback(code, state_payload), SIGNIN_COOKIE)


def _signin_callback(code: str | None, state_payload: dict) -> flask.Response:
    if not signin_ready():
        return sign_in_error("slack_unavailable")
    if not code:
        return sign_in_error("slack_cancelled")
    # Before the code is exchanged, so a forwarded link can't spend it.
    if not started_in_this_browser(SIGNIN_COOKIE, state_payload["nonce"]):
        logger.warning("[SLACK-SIGNIN] state was not started in this browser")
        return sign_in_error("slack_expired")

    identity = _oidc_identity(code, state_payload["nonce"])
    if identity is None:
        return sign_in_error("slack_identity")

    user_id = find_user_by_slack_identity(identity["team_id"], identity["slack_user_id"])
    if user_id:
        logger.info("[SLACK-SIGNIN] linked account signed in user=%s", user_id)
        return handoff_redirect(user_id, via="slack_signin")

    joined = join_workspace_org(identity)
    if joined is None:
        # Nobody in this workspace uses Aurora yet: offer to add it.
        return sign_in_error("slack_workspace_not_connected")
    if not isinstance(joined, str):
        return joined
    return handoff_redirect(joined, via="slack_join")
