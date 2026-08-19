"""One-click GitHub App signup flow (hosted deployments only).

Lets a visitor on the marketing website deploy Aurora's Incident
Prevention bot AND get an Aurora account in a single GitHub screen:

    GET /github/app/signup/start     -> 302 to GitHub's App install page
    GET /github/app/signup/callback  -> provisions org+user, links the
                                        installation, auto-enrolls repos in
                                        Incident Prevention, then redirects
                                        to the app with a one-time handoff
                                        token that logs the browser in.

Requires the GitHub App setting "Request user authorization (OAuth)
during installation" — the combined Install & Authorize screen is what
makes the callback carry BOTH ``installation_id`` (what to watch) and an
OAuth ``code`` (who the user is). This endpoint must be registered as a
Callback URL in the App settings.

The whole flow is gated on ``HOSTED_SIGNUP_ENABLED`` (default off):
self-hosted/OSS deployments keep invite-only registration and these
routes 404-equivalent (hard-coded error, zero DB writes).

Anti-spoofing invariants (mirrors ``github_app.py`` — do NOT relax):

    1. ``installation_id`` MUST be verified via ``GET
       /app/installations/{id}`` with the App JWT before any DB write.
    2. ``state`` MUST verify under the signup salt (signed + expiring).
       It carries no user (none exists yet); it proves the flow started
       at ``/signup/start`` and bounds its age.
    3. The user's identity comes ONLY from exchanging the OAuth ``code``
       with GitHub — never from query params.
    4. Error templates render HARD-CODED strings only.
    5. An existing Aurora account is auto-logged-in ONLY when matched by
       ``github_user_id`` (previously proven ownership). An email-only
       collision redirects to sign-in instead — a GitHub account whose
       email matches an Aurora account must not become a login bypass.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import uuid as uuid_mod
from datetime import datetime, timedelta

import bcrypt
import flask
import requests
from flask import Blueprint, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from utils.auth.github_app_jwt import GitHubAppJWTError, mint_app_jwt
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

github_signup_bp = Blueprint("github_signup", __name__)

FRONTEND_URL = os.getenv("FRONTEND_URL") or ""
GITHUB_TIMEOUT = 20
_GH_JSON_MEDIA_TYPE = "application/vnd.github+json"

_SIGNUP_STATE_TTL_SEC = 30 * 60
_SIGNUP_STATE_SALT = "aurora.github.app.signup-state.v1"

# One-time handoff token: minted after provisioning, redeemed exactly once
# by the frontend's NextAuth handler within this window.
HANDOFF_TTL_SEC = 120

# Hard-coded user-facing error strings. NEVER substitute query params.
_ERROR_NOT_AVAILABLE = "One-click signup is not available on this deployment"
_ERROR_MISSING_PARAMS = "Missing required parameters from GitHub callback"
_ERROR_INVALID_STATE = "Signup request could not be verified. Please try again from the website."
_ERROR_BAD_INSTALL_ID = "GitHub installation could not be verified"
_ERROR_GITHUB_API = "Could not verify installation with GitHub"
_ERROR_IDENTITY = "Could not verify your GitHub identity"
_ERROR_INTERNAL = "An internal error occurred while creating your account"

_SLUG_SANITIZE_RE = re.compile(r"[^a-z0-9]+")


def is_hosted_signup_enabled() -> bool:
    """One-click website signup — hosted deployments only, default OFF.

    OSS/self-hosted installs keep invite-only registration; flipping this
    on requires the operator to have configured the GitHub App with
    request-user-authorization and a client secret.
    """
    return os.getenv("HOSTED_SIGNUP_ENABLED", "false").lower() == "true"


def _signup_ready() -> bool:
    return (
        is_hosted_signup_enabled()
        and bool(flask.current_app.config.get("GITHUB_APP_ENABLED"))
        and bool(os.getenv("GITHUB_APP_CLIENT_SECRET"))
    )


def _state_serializer() -> URLSafeTimedSerializer:
    secret = os.getenv("FLASK_SECRET_KEY") or flask.current_app.secret_key
    if not secret:
        raise RuntimeError(
            "FLASK_SECRET_KEY is not configured; cannot sign signup state"
        )
    return URLSafeTimedSerializer(secret, salt=_SIGNUP_STATE_SALT)


def _sign_signup_state() -> str:
    """Signed, expiring proof that the flow started at ``/signup/start``.

    No user is bound (none exists yet); the nonce keeps tokens unique so a
    leaked one can't be recognized by value.
    """
    return _state_serializer().dumps({"flow": "signup", "nonce": secrets.token_urlsafe(8)})


def _verify_signup_state(state: str) -> bool:
    try:
        payload = _state_serializer().loads(state, max_age=_SIGNUP_STATE_TTL_SEC)
    except SignatureExpired:
        logger.warning("[GITHUB-SIGNUP] state expired")
        return False
    except BadSignature:
        logger.warning("[GITHUB-SIGNUP] state failed signature check")
        return False
    except Exception:
        logger.warning("[GITHUB-SIGNUP] state could not be parsed")
        return False
    return isinstance(payload, dict) and payload.get("flow") == "signup"


def _render_error(reason: str) -> flask.Response:
    return flask.make_response(
        flask.render_template(
            "github_callback_error.html",
            error=reason,
            frontend_url=FRONTEND_URL,
        )
    )


@github_signup_bp.route("/app/signup/start", methods=["GET"])
def github_app_signup_start():
    """Unauthenticated entry point for the website's one-click CTA.

    302s straight into GitHub's Install & Authorize screen — the ONLY
    screen the visitor sees before landing in Aurora logged in.
    """
    if not _signup_ready():
        return _render_error(_ERROR_NOT_AVAILABLE)

    slug = (os.getenv("NEXT_PUBLIC_GITHUB_APP_SLUG") or "").strip()
    if not slug:
        logger.error("[GITHUB-SIGNUP] NEXT_PUBLIC_GITHUB_APP_SLUG missing")
        return _render_error(_ERROR_NOT_AVAILABLE)

    try:
        state = _sign_signup_state()
    except RuntimeError:
        logger.exception("[GITHUB-SIGNUP] failed to sign signup state")
        return _render_error(_ERROR_INTERNAL)

    return flask.redirect(
        f"https://github.com/apps/{slug}/installations/new?state={state}", code=302
    )


def _verify_installation(installation_id: int) -> dict | None:
    """Verify ``installation_id`` belongs to this App; return its metadata.

    None means spoofed/unreachable — the caller must write ZERO rows.
    """
    try:
        app_jwt = mint_app_jwt()
    except GitHubAppJWTError:
        logger.exception("[GITHUB-SIGNUP] App JWT mint failed")
        return None
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
        logger.exception("[GITHUB-SIGNUP] installation verify request failed")
        return None
    if resp.status_code != 200:
        logger.warning(
            "[GITHUB-SIGNUP] installation verify returned status=%d", resp.status_code
        )
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _exchange_code_for_identity(code: str) -> dict | None:
    """Exchange the install-time OAuth ``code`` for the GitHub user identity.

    Returns ``{"id": int, "login": str, "name": str|None, "email": str}``
    or None. The email is GitHub's verified primary when available, else
    the ``{id}+{login}@users.noreply.github.com`` convention.
    """
    from connectors.github_connector.config import load_github_app_config

    client_id = load_github_app_config().client_id
    client_secret = os.getenv("GITHUB_APP_CLIENT_SECRET", "")

    try:
        token_resp = requests.post(
            "https://github.com/login/oauth/access_token",
            json={"client_id": client_id, "client_secret": client_secret, "code": code},
            headers={"Accept": "application/json"},
            timeout=GITHUB_TIMEOUT,
        )
    except requests.RequestException:
        logger.exception("[GITHUB-SIGNUP] code exchange request failed")
        return None
    if token_resp.status_code != 200:
        logger.warning(
            "[GITHUB-SIGNUP] code exchange returned status=%d", token_resp.status_code
        )
        return None
    try:
        access_token = (token_resp.json() or {}).get("access_token")
    except ValueError:
        access_token = None
    if not access_token:
        logger.warning("[GITHUB-SIGNUP] code exchange returned no access_token")
        return None

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": _GH_JSON_MEDIA_TYPE,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        user_resp = requests.get(
            "https://api.github.com/user", headers=headers, timeout=GITHUB_TIMEOUT
        )
    except requests.RequestException:
        logger.exception("[GITHUB-SIGNUP] /user request failed")
        return None
    if user_resp.status_code != 200:
        return None
    try:
        user = user_resp.json()
    except ValueError:
        return None
    if not isinstance(user, dict):
        return None

    gh_id = user.get("id")
    login = user.get("login")
    if not isinstance(gh_id, int) or not isinstance(login, str) or not login:
        return None

    email = user.get("email") if isinstance(user.get("email"), str) else None
    if not email:
        # Private-email profiles: /user carries null; ask /user/emails for
        # the verified primary. Scope-restricted tokens may 404 — fall back
        # to GitHub's stable noreply convention rather than failing signup.
        try:
            emails_resp = requests.get(
                "https://api.github.com/user/emails",
                headers=headers,
                timeout=GITHUB_TIMEOUT,
            )
            if emails_resp.status_code == 200:
                entries = emails_resp.json()
                if isinstance(entries, list):
                    for entry in entries:
                        if (
                            isinstance(entry, dict)
                            and entry.get("primary")
                            and entry.get("verified")
                            and isinstance(entry.get("email"), str)
                        ):
                            email = entry["email"]
                            break
        except (requests.RequestException, ValueError):
            logger.warning("[GITHUB-SIGNUP] /user/emails lookup failed", exc_info=True)
    if not email:
        email = f"{gh_id}+{login}@users.noreply.github.com"

    name = user.get("name") if isinstance(user.get("name"), str) else None
    return {"id": gh_id, "login": login, "name": name or login, "email": email}


def _unique_org_identity(cur, base_name: str) -> tuple[str, str]:
    """Return an (org_name, slug) pair not colliding with existing orgs."""
    name = base_name[:100]
    slug = _SLUG_SANITIZE_RE.sub("-", base_name.lower()).strip("-")[:50]
    if len(slug) < 2:
        slug = slug + "-org"

    cur.execute("SELECT 1 FROM organizations WHERE LOWER(name) = LOWER(%s)", (name,))
    if cur.fetchone():
        suffix = uuid_mod.uuid4().hex[:6]
        name = f"{base_name[:90]}-{suffix}"
        slug = f"{slug[:42]}-{suffix}"
    else:
        cur.execute("SELECT 1 FROM organizations WHERE slug = %s", (slug,))
        if cur.fetchone():
            slug = f"{slug[:42]}-{uuid_mod.uuid4().hex[:6]}"
    return name, slug


def _mint_handoff(cur, user_id: str) -> str:
    """Store a hashed one-time login token on the user row; return the raw token."""
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    cur.execute(
        """UPDATE users
              SET signup_handoff_hash = %s,
                  signup_handoff_expires_at = %s
            WHERE id = %s""",
        (token_hash, datetime.now() + timedelta(seconds=HANDOFF_TTL_SEC), user_id),
    )
    return token


def _link_installation(cur, user_id: str, org_id: str | None, install_data: dict) -> None:
    """UPSERT ``github_installations`` + the user join row (same SQL contract
    as the authenticated install callback)."""
    account = install_data.get("account") or {}
    if not isinstance(account, dict):
        account = {}
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
            install_data["id"],
            account.get("login"),
            account.get("id"),
            account.get("type"),
            install_data.get("target_type") or account.get("type"),
            json.dumps(install_data.get("permissions") or {}),
            json.dumps(install_data.get("events") or []),
            install_data.get("repository_selection") or "selected",
            install_data.get("suspended_at"),
        ),
    )
    cur.execute(
        """INSERT INTO user_github_installations (user_id, org_id, installation_id)
           VALUES (%s, %s, %s)
           ON CONFLICT (user_id, installation_id) DO UPDATE SET
                disconnected_at = NULL,
                org_id = EXCLUDED.org_id""",
        (user_id, org_id, install_data["id"]),
    )


def _validate_install_metadata(install_data: dict) -> bool:
    """Schema gate mirroring the authenticated callback's field checks."""
    account = install_data.get("account") or {}
    if not isinstance(account, dict):
        return False
    return (
        isinstance(install_data.get("id"), int)
        and isinstance(account.get("login"), str)
        and isinstance(account.get("id"), int)
        and account.get("type") in ("User", "Organization")
    )


def _provision_and_handoff(identity: dict, install_data: dict):
    """Find-or-create the Aurora account, link the install, mint the handoff.

    Returns ``(redirect_response, user_id, installation_id)`` on success or
    ``(error_response, None, None)``.
    """
    gh_id, login = identity["id"], identity["login"]
    installation_id = install_data["id"]

    conn = db_pool.get_admin_connection()
    try:
        with conn as held_conn:
            with held_conn.cursor() as cur:
                # users/organizations are not RLS-protected (matches register).
                cur.execute(
                    "SELECT id FROM users WHERE github_user_id = %s", (gh_id,)
                )
                row = cur.fetchone()
                created = False

                if row:
                    user_id = row[0]
                    cur.execute(
                        "UPDATE users SET github_login = %s WHERE id = %s",
                        (login, user_id),
                    )
                    cur.execute(
                        "SELECT org_id FROM users WHERE id = %s", (user_id,)
                    )
                    org_row = cur.fetchone()
                    org_id = org_row[0] if org_row else None
                else:
                    cur.execute(
                        "SELECT 1 FROM users WHERE email = %s", (identity["email"],)
                    )
                    if cur.fetchone():
                        # Email belongs to an Aurora account never linked to
                        # this GitHub identity — auto-login here would be an
                        # account-takeover primitive (invariant #5).
                        logger.info(
                            "[GITHUB-SIGNUP] email collision with unlinked account; "
                            "redirecting to sign-in"
                        )
                        return (
                            flask.redirect(
                                f"{FRONTEND_URL}/sign-in?error=account_exists",
                                code=302,
                            ),
                            None,
                            None,
                        )

                    created = True
                    # Unusable password: random bytes hashed and discarded.
                    # GitHub-provisioned users log in via handoff (and can set
                    # a password later through the normal reset flow).
                    unusable = bcrypt.hashpw(os.urandom(32), bcrypt.gensalt())
                    cur.execute(
                        """INSERT INTO users (email, password_hash, name, role,
                                              email_verified, github_user_id,
                                              github_login, created_at)
                           VALUES (%s, %s, %s, 'admin', TRUE, %s, %s, NOW())
                           RETURNING id""",
                        (
                            identity["email"],
                            unusable.decode("utf-8"),
                            identity["name"],
                            gh_id,
                            login,
                        ),
                    )
                    user_id = cur.fetchone()[0]

                    account = install_data.get("account") or {}
                    base_name = (
                        account.get("login")
                        if isinstance(account, dict) and account.get("login")
                        else login
                    )
                    org_name, slug = _unique_org_identity(cur, base_name)
                    cur.execute(
                        """INSERT INTO organizations (id, name, slug, created_by)
                           VALUES (gen_random_uuid()::TEXT, %s, %s, %s)
                           RETURNING id""",
                        (org_name, slug, user_id),
                    )
                    org_id = cur.fetchone()[0]
                    cur.execute(
                        "UPDATE users SET org_id = %s WHERE id = %s",
                        (org_id, user_id),
                    )

                _link_installation(cur, user_id, org_id, install_data)
                handoff_token = _mint_handoff(cur, user_id)
                held_conn.commit()
    except Exception:
        logger.exception("[GITHUB-SIGNUP] provisioning transaction failed")
        return (_render_error(_ERROR_INTERNAL), None, None)

    if created:
        # Same post-registration seeding as /api/auth/register — all
        # best-effort, none may block the redirect.
        try:
            from utils.auth.enforcer import assign_role_to_user

            assign_role_to_user(user_id, "admin", org_id)
        except Exception:
            logger.warning("[GITHUB-SIGNUP] Casbin role assignment failed", exc_info=True)
        try:
            from utils.auth.command_policy import seed_default_command_policy

            seed_default_command_policy(org_id, user_id)
        except Exception:
            logger.warning("[GITHUB-SIGNUP] command policy seeding failed", exc_info=True)
        try:
            from routes.audit_routes import record_audit_event

            record_audit_event(
                org_id, user_id, "register", "organization", org_id,
                {"via": "github_one_click"}, request,
            )
        except Exception:
            logger.warning("[GITHUB-SIGNUP] audit event failed", exc_info=True)

    try:
        from utils.auth.tool_registry import seed_org_tool_permissions

        seed_org_tool_permissions(org_id, user_id)
    except Exception:
        logger.warning("[GITHUB-SIGNUP] tool permission seeding failed", exc_info=True)

    redirect = flask.redirect(
        f"{FRONTEND_URL}/sign-in?handoff={handoff_token}", code=302
    )
    return (redirect, user_id, installation_id)


@github_signup_bp.route("/app/signup/callback", methods=["GET"])
def github_app_signup_callback():
    """Public callback for the one-click signup install.

    GitHub (with request-user-authorization enabled) redirects here with
    ``code`` + ``installation_id`` + ``setup_action`` + ``state``.
    """
    if not _signup_ready():
        return _render_error(_ERROR_NOT_AVAILABLE)

    installation_id_raw = (request.args.get("installation_id") or "").strip()
    state = (request.args.get("state") or "").strip()
    code = (request.args.get("code") or "").strip()

    if not installation_id_raw or not state or not code:
        logger.warning("[GITHUB-SIGNUP] callback missing required params")
        return _render_error(_ERROR_MISSING_PARAMS)

    if not _verify_signup_state(state):
        return _render_error(_ERROR_INVALID_STATE)

    try:
        installation_id = int(installation_id_raw)
    except ValueError:
        logger.warning("[GITHUB-SIGNUP] non-integer installation_id rejected")
        return _render_error(_ERROR_BAD_INSTALL_ID)
    if installation_id <= 0:
        return _render_error(_ERROR_BAD_INSTALL_ID)

    install_data = _verify_installation(installation_id)
    if install_data is None:
        return _render_error(_ERROR_GITHUB_API)
    if install_data.get("id") != installation_id or not _validate_install_metadata(
        install_data
    ):
        logger.warning("[GITHUB-SIGNUP] installation metadata failed validation")
        return _render_error(_ERROR_BAD_INSTALL_ID)

    identity = _exchange_code_for_identity(code)
    if identity is None:
        return _render_error(_ERROR_IDENTITY)

    response, user_id, linked_installation_id = _provision_and_handoff(
        identity, install_data
    )
    if user_id is None:
        return response

    logger.info(
        "[GITHUB-SIGNUP] provisioned user=%s installation_id=%d gh_login=%s",
        user_id, linked_installation_id, identity["login"],
    )

    # Auto-import granted repos WITH Incident Prevention enabled — the user
    # already curated the repo list on GitHub's install screen; this is what
    # makes the flow genuinely one-click. Best-effort: the connectors page
    # remains the fallback.
    try:
        from routes.github.github_repo_metadata import import_installation_repos

        import_installation_repos.delay(
            user_id, linked_installation_id, enroll_change_gating=True
        )
    except Exception:
        logger.warning(
            "[GITHUB-SIGNUP] failed to enqueue repo auto-import installation_id=%d",
            linked_installation_id,
            exc_info=True,
        )

    return response
