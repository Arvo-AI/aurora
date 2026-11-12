"""One-click GitHub App signup flow (hosted deployments only).

Lets a visitor on the marketing website deploy Aurora's Incident
Prevention bot AND get an Aurora account in a single GitHub screen:

    GET /github/app/signup/start     -> 302 to GitHub's App install page
    GET /github/app/signup/callback  -> provisions org+user, links the
                                        installation, auto-enrolls repos in
                                        Incident Prevention, then redirects
                                        to the app with a one-time handoff
                                        token that logs the browser in.
    GET /github/app/signup/complete  -> a brand-new installer's account is
                                        created here, one hop later, so a
                                        browser already signed in can link
                                        the install to that org instead
                                        (``GET /github/app/link-pending/confirm``).

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
    2. ``state`` is attribution only. Installs that start anywhere else
       (the App's GitHub page, a README link, an expired tab) carry no
       signup state and still get an account, because invariants 1, 3 and
       6 are what bind the installation to the person: GitHub vouches for
       the installation, the OAuth code names the user, and GitHub confirms
       the user can access that installation.
    3. The user's identity comes ONLY from exchanging the OAuth ``code``
       with GitHub — never from query params.
    4. Error templates render HARD-CODED strings only.
    5. An existing Aurora account is auto-logged-in ONLY when matched by
       ``github_user_id`` (previously proven ownership). An email-only
       collision redirects to sign-in instead — a GitHub account whose
       email matches an Aurora account must not become a login bypass.
    6. The OAuth user MUST be authorized for the ``installation_id`` they
       present (``GET /user/installations`` with their token lists it).
       Installation ids are sequential and guessable; without this check a
       caller could swap in another org's installation and have Aurora link
       it — and import its repos — under the caller's account.

Org members without admin rights can only *request* an install. GitHub then
sends ``setup_action=request`` with no ``installation_id``; that lands on a
notice page instead of an error.

Interaction with the authenticated in-app install flow: once "Request user
authorization (OAuth) during installation" is on, GitHub ignores the App's
Setup URL and sends EVERY install (website signup AND logged-in users
clicking "Install" inside Aurora) to the App's FIRST registered Callback
URL. So this callback must be that first URL, and it dispatches on the
``state`` salt: signup-salted states run the flow below, install-salted
states are handed to ``github_app.github_app_install_callback`` unchanged.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import uuid as uuid_mod
from urllib.parse import urlencode

import bcrypt
import flask
import requests
from flask import Blueprint, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from utils.auth.github_app_jwt import GitHubAppJWTError, mint_app_jwt
from utils.auth import normalize_email
from utils.auth.handoff import mint_handoff as _mint_handoff
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

github_signup_bp = Blueprint("github_signup", __name__)

FRONTEND_URL = os.getenv("FRONTEND_URL") or ""
GITHUB_TIMEOUT = 20
_GH_JSON_MEDIA_TYPE = "application/vnd.github+json"

_SIGNUP_STATE_TTL_SEC = 30 * 60
_SIGNUP_STATE_SALT = "aurora.github.app.signup-state.v1"
# Pending link for an installer whose GitHub email already has an Aurora
# account: carried through password sign-in, redeemed by /app/claim-pending.
_CLAIM_TOKEN_TTL_SEC = 30 * 60
_CLAIM_TOKEN_SALT = "aurora.github.app.pending-claim.v1"
# A brand-new installer is created on a second hop through the app, so a
# browser already signed in to Aurora can attach the install to that org
# instead of getting a second account. Single use, short-lived.
_PENDING_SIGNUP_TTL_SEC = 10 * 60
_PENDING_SIGNUP_SALT = "aurora.github.app.pending-signup.v1"
_PENDING_SIGNUP_USED_KEY = "github:pending-signup-used:"
_PENDING_BIND_COOKIE = "aurora_gh_pending"
_PENDING_BIND_PATH = "/github/app"

# Hard-coded user-facing error strings. NEVER substitute query params.
_ERROR_NOT_AVAILABLE = "One-click signup is not available on this deployment"
_ERROR_MISSING_PARAMS = "Missing required parameters from GitHub callback"
_ERROR_BAD_INSTALL_ID = "GitHub installation could not be verified"
_ERROR_GITHUB_API = "Could not verify installation with GitHub"
_ERROR_IDENTITY = "Could not verify your GitHub identity"
_ERROR_INSTALL_NOT_AUTHORIZED = (
    "Your GitHub account does not have access to this installation"
)
_ERROR_INTERNAL = "An internal error occurred while creating your account"
_ERROR_LINK_EXPIRED = (
    "This GitHub install link expired or was opened in a different browser. "
    "Install Aurora again from Connectors."
)

# GET /user/installations pagination bound. 100 per page is GitHub's max;
# 10 pages (1,000 installations) is far beyond any real user and keeps a
# hostile/looping API from turning one callback into unbounded requests.
_USER_INSTALLATIONS_PER_PAGE = 100
_USER_INSTALLATIONS_MAX_PAGES = 10

_SLUG_SANITIZE_RE = re.compile(r"[^a-z0-9]+")

# Marketing attribution the website appends to the CTA (analytics distinct id +
# UTMs). Carried inside the signed state and echoed onto the final app redirect
# so the frontend can tie the signup to the site visit. Never used for auth.
_ATTRIBUTION_KEYS = ("ph_did", "utm_source", "utm_medium", "utm_campaign", "utm_content")
_ATTRIBUTION_VALUE_RE = re.compile(r"[A-Za-z0-9._/-]{1,100}")


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


def _state_serializer(salt: str = _SIGNUP_STATE_SALT) -> URLSafeTimedSerializer:
    secret = os.getenv("FLASK_SECRET_KEY") or flask.current_app.secret_key
    if not secret:
        raise RuntimeError(
            "FLASK_SECRET_KEY is not configured; cannot sign signup state"
        )
    return URLSafeTimedSerializer(secret, salt=salt)


def _sign_claim_token(installation_id: int, identity: dict) -> str:
    """Signed, expiring note that ``identity`` installed ``installation_id``.

    Minted only after invariants 1, 3 and 6 passed. The email is the one the
    collision matched, so redemption can require the signed-in account to
    own that same address.
    """
    return _state_serializer(_CLAIM_TOKEN_SALT).dumps({
        "iid": installation_id,
        "gh_id": identity["id"],
        "gh_login": identity["login"],
        "email": identity["email"],
    })


def verify_claim_token(token: str) -> dict | None:
    """Payload of a valid, unexpired claim token, else None."""
    try:
        payload = _state_serializer(_CLAIM_TOKEN_SALT).loads(
            token, max_age=_CLAIM_TOKEN_TTL_SEC
        )
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if not (
        isinstance(payload.get("iid"), int)
        and isinstance(payload.get("gh_id"), int)
        and isinstance(payload.get("gh_login"), str)
        and isinstance(payload.get("email"), str)
    ):
        return None
    return payload


def _sign_pending_signup(
    installation_id: int, identity: dict, attribution: dict, account_login: str, bind: str
) -> str:
    """Signed note that a verified, brand-new installer is waiting for an account.

    Minted only after invariants 1, 3 and 6 passed. ``bind`` is the random
    value of the cookie set on the installer's browser; only its hash goes
    in the token, so the token alone (forwarded, or read from a log) cannot
    be redeemed anywhere else.
    """
    return _state_serializer(_PENDING_SIGNUP_SALT).dumps({
        "iid": installation_id,
        "acct": account_login,
        "identity": {k: identity[k] for k in ("id", "login", "name", "email")},
        "attr": attribution or {},
        "b": hashlib.sha256(bind.encode()).hexdigest(),
    })


def verify_pending_signup(token: str | None) -> dict | None:
    """Payload of a valid, unexpired pending-signup token, else None."""
    if not isinstance(token, str) or not token:
        return None
    try:
        payload = _state_serializer(_PENDING_SIGNUP_SALT).loads(
            token, max_age=_PENDING_SIGNUP_TTL_SEC
        )
    except Exception:
        return None
    identity = payload.get("identity") if isinstance(payload, dict) else None
    if not (
        isinstance(payload.get("iid"), int)
        and isinstance(payload.get("b"), str)
        and isinstance(identity, dict)
        and isinstance(identity.get("id"), int)
        and isinstance(identity.get("login"), str)
        and isinstance(identity.get("email"), str)
    ):
        return None
    attr = payload.get("attr")
    payload["attr"] = _attribution_from(attr) if isinstance(attr, dict) else {}
    return payload


def pending_bound_to_this_browser(payload: dict) -> bool:
    """Whether the request carries the cookie set where the install happened.

    Stops a pending signup link from working in any other browser: opened
    signed out it would log that browser in as the installer (login CSRF),
    opened signed in it would connect the installer's install to the wrong
    org.
    """
    cookie = request.cookies.get(_PENDING_BIND_COOKIE) or ""
    digest = hashlib.sha256(cookie.encode()).hexdigest()
    return bool(cookie) and secrets.compare_digest(digest, payload["b"])


def _replay_key(token: str) -> str:
    return _PENDING_SIGNUP_USED_KEY + hashlib.sha256(token.encode()).hexdigest()


def reserve_pending_signup(token: str) -> bool:
    """Mark ``token`` in use; False if it was already used.

    Without Redis (or if the call fails) this allows the request: the
    browser cookie is still required and is cleared once the token is spent,
    so a replay needs the installer's own browser within the 10 minutes.
    """
    try:
        from utils.cache.redis_client import get_redis_client

        rc = get_redis_client()
        if rc is None:
            return True
        if not rc.set(_replay_key(token), "1", nx=True, ex=_PENDING_SIGNUP_TTL_SEC + 60):
            logger.warning("[GITHUB-SIGNUP] pending signup token replayed")
            return False
    except Exception:
        logger.warning("[GITHUB-SIGNUP] pending signup replay check unavailable", exc_info=True)
    return True


def release_pending_signup(token: str) -> None:
    """Undo :func:`reserve_pending_signup` after a failure, so a retry works."""
    try:
        from utils.cache.redis_client import get_redis_client

        rc = get_redis_client()
        if rc is not None:
            rc.delete(_replay_key(token))
    except Exception:
        logger.warning("[GITHUB-SIGNUP] pending signup release failed", exc_info=True)


def clear_pending_cookie(response: flask.Response) -> flask.Response:
    response.delete_cookie(_PENDING_BIND_COOKIE, path=_PENDING_BIND_PATH)
    return response


def _sign_signup_state(attribution: dict | None = None) -> str:
    """Signed, expiring proof that the flow started at ``/signup/start``.

    No user is bound (none exists yet); the nonce keeps tokens unique so a
    leaked one can't be recognized by value.
    """
    payload = {"flow": "signup", "nonce": secrets.token_urlsafe(8)}
    if attribution:
        payload["attr"] = attribution
    return _state_serializer().dumps(payload)


def _attribution_from(params) -> dict:
    """Allow-listed, format-checked attribution params; anything else is dropped."""
    return {
        k: v for k in _ATTRIBUTION_KEYS
        if isinstance(v := params.get(k), str) and _ATTRIBUTION_VALUE_RE.fullmatch(v)
    }


def _signup_state_attribution(state: str) -> dict:
    """Attribution carried by an already-verified signup state."""
    try:
        payload = _state_serializer().loads(state, max_age=_SIGNUP_STATE_TTL_SEC)
    except Exception:
        return {}
    attr = payload.get("attr") if isinstance(payload, dict) else None
    return _attribution_from(attr) if isinstance(attr, dict) else {}


def _with_attribution(response: flask.Response, attribution: dict) -> flask.Response:
    """Append attribution to a redirect into the app; other responses pass through."""
    location = response.headers.get("Location") or ""
    if attribution and response.status_code == 302 and location.startswith(f"{FRONTEND_URL}/"):
        response.headers["Location"] = f"{location}&{urlencode(attribution)}"
    return response


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
        state = _sign_signup_state(_attribution_from(request.args))
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


def _exchange_code_for_identity(code: str) -> tuple[dict, str] | None:
    """Exchange the install-time OAuth ``code`` for the GitHub user identity.

    Returns ``(identity, access_token)`` where identity is
    ``{"id": int, "login": str, "name": str|None, "email": str}``, or None.
    The email is GitHub's verified primary when available, else the
    ``{id}+{login}@users.noreply.github.com`` convention. The user access
    token is returned so the caller can prove the user is authorized for
    the installation (invariant #6); it is never persisted.
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
    # Normalize before it reaches the collision check and the INSERT: GitHub
    # returns whatever case is on the profile, and a mixed-case spelling must
    # not be able to provision a second row for an existing Aurora account.
    identity = {
        "id": gh_id,
        "login": login,
        "name": name or login,
        "email": normalize_email(email),
    }
    return identity, access_token


def _user_authorized_for_installation(access_token: str, installation_id: int) -> bool:
    """True iff ``GET /user/installations`` (as the OAuth user) lists ``installation_id``.

    GitHub only returns installations the token's user can access (owner,
    collaborator, or org member with permission), so presence here proves
    the installer is entitled to the installation they presented. Any
    failure is a rejection — this gate must fail closed.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": _GH_JSON_MEDIA_TYPE,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    for page in range(1, _USER_INSTALLATIONS_MAX_PAGES + 1):
        try:
            resp = requests.get(
                "https://api.github.com/user/installations",
                headers=headers,
                params={"per_page": _USER_INSTALLATIONS_PER_PAGE, "page": page},
                timeout=GITHUB_TIMEOUT,
            )
        except requests.RequestException:
            logger.exception("[GITHUB-SIGNUP] /user/installations request failed")
            return False
        if resp.status_code != 200:
            logger.warning(
                "[GITHUB-SIGNUP] /user/installations returned status=%d", resp.status_code
            )
            return False
        try:
            payload = resp.json()
        except ValueError:
            return False
        installations = payload.get("installations") if isinstance(payload, dict) else None
        if not isinstance(installations, list):
            return False
        if any(
            isinstance(inst, dict) and inst.get("id") == installation_id
            for inst in installations
        ):
            return True
        if len(installations) < _USER_INSTALLATIONS_PER_PAGE:
            break
    logger.warning(
        "[GITHUB-SIGNUP] OAuth user is not authorized for installation_id=%d",
        installation_id,
    )
    return False


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


def _linked_elsewhere(cur, installation_id: int, org_id: str | None) -> bool:
    """True when an org other than ``org_id`` has this installation linked.

    The callback proves the installer can see the installation, not that
    they own the Aurora org that linked it. Linking it again would hand a
    new org every repo the installation covers, private ones included.
    """
    # Held until the caller's transaction ends, so two orgs linking the same
    # installation at once cannot both see it unowned.
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (installation_id,))
    cur.execute(
        """SELECT DISTINCT org_id
             FROM user_github_installations
            WHERE installation_id = %s
              AND disconnected_at IS NULL""",
        (installation_id,),
    )
    owners = {row[0] for row in cur.fetchall()}
    return bool(owners) and org_id not in owners


def _linked_elsewhere_redirect() -> flask.Response:
    # Usually the owning org's own member (an in-app installer changing the
    # repo selection on GitHub): their session, or sign-in, takes it from
    # Connectors.
    logger.warning("[GITHUB-SIGNUP] installation already linked to another org; not provisioning")
    return flask.redirect(f"{FRONTEND_URL}/connectors?installed=github", code=302)


def _pending_signup_redirect(
    identity: dict, install_data: dict, attribution: dict | None
) -> flask.Response:
    bind = secrets.token_urlsafe(24)
    account = install_data.get("account") or {}
    token = _sign_pending_signup(
        install_data["id"], identity, attribution or {},
        account.get("login") if isinstance(account, dict) else "", bind,
    )
    response = flask.redirect(f"{FRONTEND_URL}/sign-in?{urlencode({'gh_new': token})}", code=302)
    response.set_cookie(
        _PENDING_BIND_COOKIE, bind,
        max_age=_PENDING_SIGNUP_TTL_SEC, path=_PENDING_BIND_PATH,
        httponly=True, samesite="Lax",
        secure=request.is_secure or FRONTEND_URL.startswith("https://"),
    )
    return response


def _provision_and_handoff(
    identity: dict,
    install_data: dict,
    confirmed: bool = False,
    attribution: dict | None = None,
):
    """Find-or-create the Aurora account, link the install, mint the handoff.

    Returns ``(redirect_response, user_id, installation_id, created)`` on
    success or ``(error_response, None, None, False)``.

    A brand-new installer is not created until ``confirmed``: first the
    browser goes to ``/sign-in?gh_new=<token>``. Signed out, the app sends it
    straight on to ``/signup/complete`` (no click). Signed in, the person is
    offered to connect the install to their current org instead of getting
    a second account.
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
                    if _linked_elsewhere(cur, installation_id, org_id):
                        return (_linked_elsewhere_redirect(), None, None, False)
                else:
                    # Normalized comparison so an existing mixed-case account
                    # is still detected as a collision rather than duplicated.
                    cur.execute(
                        "SELECT 1 FROM users WHERE LOWER(email) = %s", (identity["email"],)
                    )
                    if cur.fetchone():
                        # Email belongs to an Aurora account never linked to
                        # this GitHub identity — auto-login here would be an
                        # account-takeover primitive (invariant #5).
                        # Signing in with that account's password proves the
                        # rest; the claim token lets sign-in finish the link.
                        logger.info(
                            "[GITHUB-SIGNUP] email collision with unlinked account; "
                            "redirecting to sign-in with a pending claim"
                        )
                        claim = _sign_claim_token(installation_id, identity)
                        return (
                            flask.redirect(
                                f"{FRONTEND_URL}/sign-in?error=account_exists"
                                f"&gh_claim={claim}",
                                code=302,
                            ),
                            None,
                            None,
                            False,
                        )

                    if _linked_elsewhere(cur, installation_id, None):
                        return (_linked_elsewhere_redirect(), None, None, False)
                    if not confirmed:
                        return (_pending_signup_redirect(identity, install_data, attribution),
                                None, None, False)

                    created = True
                    # Unusable password: random bytes hashed and discarded.
                    # GitHub-provisioned users log in via handoff (and can set
                    # a password later through the normal reset flow).
                    unusable = bcrypt.hashpw(os.urandom(32), bcrypt.gensalt())
                    cur.execute(
                        """INSERT INTO users (email, password_hash, name, role,
                                              email_verified, must_change_password,
                                              github_user_id,
                                              github_login, created_at)
                           VALUES (%s, %s, %s, 'admin', TRUE, TRUE, %s, %s, NOW())
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
        return (_render_error(_ERROR_INTERNAL), None, None, False)

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

        from utils.hooks import get_hook

        get_hook("after_user_created")(user_id, org_id, identity["email"], "github", True)

    if org_id:
        try:
            from utils.auth.tool_registry import seed_org_tool_permissions

            seed_org_tool_permissions(org_id, user_id)
        except Exception:
            logger.warning("[GITHUB-SIGNUP] tool permission seeding failed", exc_info=True)
    else:
        # Returning user whose row predates org backfill — nothing to seed.
        logger.warning(
            "[GITHUB-SIGNUP] skipping tool permission seeding: user=%s has no org",
            user_id,
        )

    redirect = flask.redirect(
        f"{FRONTEND_URL}/sign-in?handoff={handoff_token}", code=302
    )
    return (redirect, user_id, installation_id, created)


def _response_without_install(installation_id_raw: str, code: str) -> flask.Response | None:
    """Response for a callback that cannot provision anything, else None."""
    setup_action = (request.args.get("setup_action") or "").strip()
    if setup_action == "request" and not installation_id_raw:
        # An org member asked the org's owners to approve the install.
        # Nothing exists to link yet; the owner's approval arrives later.
        logger.info("[GITHUB-SIGNUP] install requested; awaiting org owner approval")
        return flask.redirect(
            f"{FRONTEND_URL}/sign-in?notice=github_install_requested", code=302
        )
    if not installation_id_raw:
        logger.warning("[GITHUB-SIGNUP] callback missing installation_id")
        return _render_error(_ERROR_MISSING_PARAMS)
    if code:
        return None
    # Without OAuth-during-install there is no identity to provision. Repo
    # changes on an existing install also end here; send those people to
    # the app, where their session (or sign-in) takes over.
    logger.warning(
        "[GITHUB-SIGNUP] callback without OAuth code setup_action=%s",
        setup_action or "unknown",
    )
    if setup_action == "update":
        return flask.redirect(f"{FRONTEND_URL}/connectors?installed=github", code=302)
    return _render_error(_ERROR_MISSING_PARAMS)


def _attribution_state(state: str) -> str:
    """The signup state if it verifies, else "" (the install proceeds either way).

    A missing, forged or stale state is not a reason to strand a real
    install: invariants 1, 3 and 6 decide. Only attribution is lost.
    """
    if state and _verify_signup_state(state):
        return state
    logger.info(
        "[GITHUB-SIGNUP] install did not start from the website; "
        "continuing without attribution"
    )
    return ""


def _parse_installation_id(raw: str) -> int | None:
    try:
        installation_id = int(raw)
    except ValueError:
        logger.warning("[GITHUB-SIGNUP] non-integer installation_id rejected")
        return None
    return installation_id if installation_id > 0 else None


@github_signup_bp.route("/app/signup/callback", methods=["GET"])
def github_app_signup_callback():
    """Public callback for the one-click signup install.

    GitHub (with request-user-authorization enabled) redirects here with
    ``code`` + ``installation_id`` + ``setup_action`` + ``state``. Installs
    started from inside Aurora arrive here too (see module docstring) and
    are dispatched to the authenticated install handler by their state.
    """
    installation_id_raw = (request.args.get("installation_id") or "").strip()
    state = (request.args.get("state") or "").strip()
    code = (request.args.get("code") or "").strip()

    # With OAuth-during-install on, GitHub sends logged-in users' in-app
    # installs here too (first Callback URL wins, Setup URL is ignored) —
    # those carry an install-salted state bound to the Aurora user who
    # clicked Install. Hand them to the authenticated handler, which
    # re-verifies everything itself. This runs BEFORE the hosted-signup
    # gate on purpose: flipping HOSTED_SIGNUP_ENABLED off must not break
    # in-app installs while the App still points at this URL. A request-only
    # callback (no installation_id) has nothing to link and gets the notice.
    if state and installation_id_raw:
        from routes.github.github_app import (
            _verify_install_state,
            github_app_install_callback,
        )

        if _verify_install_state(state) is not None:
            logger.info("[GITHUB-SIGNUP] install-flow state; delegating to install callback")
            return github_app_install_callback()

    if not installation_id_raw and (request.args.get("setup_action") or "").strip() == "request":
        # Nothing to link or sign up; works with hosted signup off too.
        return _response_without_install(installation_id_raw, code)
    if not _signup_ready():
        return _render_error(_ERROR_NOT_AVAILABLE)

    early = _response_without_install(installation_id_raw, code)
    if early is not None:
        return early

    state = _attribution_state(state)
    installation_id = _parse_installation_id(installation_id_raw)
    if installation_id is None:
        return _render_error(_ERROR_BAD_INSTALL_ID)

    install_data = _verify_installation(installation_id)
    if install_data is None:
        return _render_error(_ERROR_GITHUB_API)
    if install_data.get("id") != installation_id or not _validate_install_metadata(
        install_data
    ):
        logger.warning("[GITHUB-SIGNUP] installation metadata failed validation")
        return _render_error(_ERROR_BAD_INSTALL_ID)

    exchanged = _exchange_code_for_identity(code)
    if exchanged is None:
        return _render_error(_ERROR_IDENTITY)
    identity, access_token = exchanged

    # Invariant #6: the installer must actually have access to the
    # installation they presented. Checked BEFORE any DB write.
    if not _user_authorized_for_installation(access_token, installation_id):
        return _render_error(_ERROR_INSTALL_NOT_AUTHORIZED)

    return _complete_signup(
        identity, install_data,
        _signup_state_attribution(state) if state else {},
        confirmed=False,
    )


def _complete_signup(
    identity: dict, install_data: dict, attribution: dict, confirmed: bool
) -> flask.Response:
    response, user_id, linked_installation_id, created = _provision_and_handoff(
        identity, install_data, confirmed=confirmed, attribution=attribution,
    )
    if "gh_new=" not in (response.headers.get("Location") or ""):
        # The pending-signup token already carries attribution.
        response = _with_attribution(response, attribution)
    if user_id is None:
        return response

    logger.info(
        "[GITHUB-SIGNUP] provisioned user=%s installation_id=%d gh_login=%s created=%s",
        user_id, linked_installation_id, identity["login"], created,
    )

    # Auto-import granted repos with PR review on: the user picked them on
    # GitHub's install screen, which is what makes the flow one-click. The
    # import runs once per link, so a returning user's login does not bring
    # back repos they removed.
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


@github_signup_bp.route("/app/signup/complete", methods=["GET"])
def github_app_signup_complete():
    """Second hop for a brand-new installer whose browser is not signed in.

    The callback already verified the installation, the installer and their
    access to it, and signed that into the token. Requires the cookie the
    callback set on this browser, re-checks the installation (it may have
    been removed or linked meanwhile), then creates the account.
    """
    if not _signup_ready():
        return _render_error(_ERROR_NOT_AVAILABLE)
    token = request.args.get("t") or ""
    pending = verify_pending_signup(token)
    if pending is None or not pending_bound_to_this_browser(pending):
        return flask.redirect(f"{FRONTEND_URL}/sign-in?error=github_signup_expired", code=302)
    if not reserve_pending_signup(token):
        return flask.redirect(f"{FRONTEND_URL}/sign-in?error=github_signup_expired", code=302)
    install_data = _verify_installation(pending["iid"])
    if install_data is None:
        release_pending_signup(token)
        return _render_error(_ERROR_GITHUB_API)
    if install_data.get("id") != pending["iid"] or not _validate_install_metadata(install_data):
        return clear_pending_cookie(_render_error(_ERROR_BAD_INSTALL_ID))
    identity = dict(pending["identity"])
    identity["name"] = identity.get("name") or identity["login"]
    response = _complete_signup(identity, install_data, pending["attr"], confirmed=True)
    if response.status_code == 302:
        return clear_pending_cookie(response)
    # An error page (e.g. a DB failure): let the same browser retry.
    release_pending_signup(token)
    return response


@github_signup_bp.route("/app/link-pending/confirm", methods=["GET"])
def github_app_link_pending_confirm():
    """First half of the card's Connect: prove this is the installer's browser.

    A top-level navigation, so it carries the cookie the callback set. It
    swaps that for a two-minute ticket and sends the browser back to the
    app, which finishes the link as whoever is signed in there
    (``POST /github/app/link-pending/finish``).
    """
    from routes.github.github_app import sign_link_ticket

    token = request.args.get("t") or ""
    pending = verify_pending_signup(token)
    if pending is None or not pending_bound_to_this_browser(pending):
        return _render_error(_ERROR_LINK_EXPIRED)
    back = urlencode({"gh_new": token, "gh_link": sign_link_ticket(token)})
    return clear_pending_cookie(flask.redirect(f"{FRONTEND_URL}/sign-in?{back}", code=302))
