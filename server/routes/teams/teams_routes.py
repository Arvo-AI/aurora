"""Microsoft Teams OAuth routes."""

import logging
import os
import secrets
import time
import uuid
from urllib.parse import quote

import jwt
import requests
from flask import Blueprint, jsonify, redirect, request

from connectors.teams_connector.client import get_teams_client_for_user
from connectors.teams_connector.oauth import exchange_code_for_token, get_auth_url, get_redirect_uri
from routes.teams.teams_channels import purge_teams_connector_data
from utils.auth.oauth2_state_cache import retrieve_oauth2_state, store_oauth2_state
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_credentials_from_db, get_org_id_for_user, get_org_preference
from utils.auth.token_management import store_tokens_in_db
from utils.log_sanitizer import sanitize
from utils.secrets.secret_ref_utils import delete_user_secret

teams_bp = Blueprint("teams", __name__)
logger = logging.getLogger(__name__)
FRONTEND_URL = os.getenv("FRONTEND_URL")


@teams_bp.route("/", methods=["GET"], strict_slashes=False)
@require_permission("connectors", "read")
def teams_status(user_id):
    try:
        creds = get_credentials_from_db(user_id, "teams")
        if not creds or not creds.get("access_token"):
            return jsonify({"connected": False})
        client = get_teams_client_for_user(user_id)
        if not client:
            return jsonify({"connected": False, "error": "Invalid or expired token"})
        me = client.get_me()
        last_bot_message_at = _last_bot_message_at(user_id)
        setup = _teams_setup_state(user_id, last_bot_message_at)
        return jsonify({
            "connected": True,
            "tenant_id": creds.get("tenant_id"),
            "team_name": creds.get("team_name"),
            "user_name": me.get("displayName"),
            "connected_at": creds.get("connected_at"),
            "incidents_channel_name": creds.get("incidents_channel_name"),
            "last_bot_message_at": last_bot_message_at,
            **setup,
        })
    except Exception:
        logger.exception("Error checking Teams status")
        return jsonify({"connected": False, "error": "Failed to check Teams status"}), 500


@teams_bp.route("/", methods=["POST"], strict_slashes=False)
@require_permission("connectors", "write")
def teams_connect(user_id):
    try:
        state = secrets.token_urlsafe(32)
        store_oauth2_state(state, user_id, "teams")
        return jsonify({
            "oauth_url": get_auth_url(state=state),
            "redirect_uri": get_redirect_uri(),
            "message": "Redirect to Microsoft for authentication",
        })
    except ValueError:
        # Missing env vars — the detail stays in the log, not in the response body
        logger.warning("Teams OAuth connect refused: configuration is incomplete", exc_info=True)
        return jsonify({
            "error": "Microsoft Teams is not configured. A developer needs to finish the Teams setup.",
            "error_code": "TEAMS_OAUTH_NOT_CONFIGURED",
        }), 503
    except Exception:
        logger.exception("Error initiating Teams OAuth")
        return jsonify({"error": "Failed to initiate Teams OAuth"}), 500


@teams_bp.route("/", methods=["DELETE"], strict_slashes=False)
@require_permission("connectors", "write")
def teams_disconnect(user_id):
    try:
        if delete_user_secret(user_id, "teams"):
            try:
                purge_teams_connector_data(user_id)
            except Exception:
                logger.warning("Teams disconnect: failed to purge local channel data", exc_info=True)
            return jsonify({"success": True, "message": "Microsoft Teams disconnected"})
        return jsonify({"error": "Failed to disconnect Microsoft Teams"}), 500
    except Exception:
        logger.exception("Error disconnecting Teams")
        return jsonify({"error": "Failed to disconnect Microsoft Teams"}), 500


def _oauth_error_code(ms_error: str, ms_desc: str) -> str:
    """A fixed code for the setup page. The raw Microsoft text never enters the redirect."""
    desc = ms_desc.lower()
    # Single-tenant app sent to the /common endpoint — only the description says so
    if "/common endpoint" in desc or "not configured as a multi-tenant" in desc:
        return "single_tenant_common"
    # Only codes the setup page has copy for
    if ms_error == "access_denied":
        return "access_denied"
    if ms_error == "invalid_request":
        return "invalid_request"
    return "oauth_error"


def _teams_oauth_failure_redirect(error_code: str) -> str:
    extra = ""
    try:
        extra = f"&redirect_uri={quote(get_redirect_uri(), safe='')}"
    except ValueError:
        pass
    return (
        f"{FRONTEND_URL}/teams/setup?teams_auth=failed"
        f"&error={quote(error_code, safe='')}{extra}"
    )


@teams_bp.route("/callback", methods=["GET"])
def teams_callback():
    try:
        ms_error = request.args.get("error")
        if ms_error:
            # Microsoft returned an error (denied consent, policy block, etc.) — no code.
            ms_error = sanitize(ms_error)[:100]
            ms_desc = sanitize((request.args.get("error_description") or "").split("\n")[0][:500])
            logger.warning(
                "Teams OAuth callback error from Microsoft: %s%s",
                ms_error,
                f" — {ms_desc}" if ms_desc else "",
            )
            return redirect(_teams_oauth_failure_redirect(_oauth_error_code(ms_error, ms_desc)))

        code = request.args.get("code")
        state = request.args.get("state")
        if not code or not state:
            logger.warning(
                "Teams OAuth callback missing code or state (has_code=%s has_state=%s)",
                bool(code),
                bool(state),
            )
            return redirect(_teams_oauth_failure_redirect("no_code_or_state"))
        state_data = retrieve_oauth2_state(state)
        if not state_data:
            return redirect(_teams_oauth_failure_redirect("invalid_state"))
        user_id = state_data.get("user_id")
        if not user_id:
            return redirect(_teams_oauth_failure_redirect("invalid_state"))
        token_data = exchange_code_for_token(code)
        access_token = token_data.get("access_token")
        if not access_token:
            return redirect(_teams_oauth_failure_redirect("no_token"))

        # The v2 token endpoint has no tenant_id field — the id_token's tid claim carries it
        tenant_id = _tenant_from_id_token(token_data.get("id_token"))
        # id_token missing or unreadable — fall back to Graph /organization
        if not tenant_id:
            tenant_id = _tenant_from_graph(access_token)
        # Without a tenant every inbound @mention is "unknown tenant", so don't report success
        if not tenant_id:
            logger.error("Teams OAuth could not resolve the tenant id; refusing to store the connection")
            return redirect(_teams_oauth_failure_redirect("tenant_unresolved"))

        teams_token_data = {
            "access_token": access_token,
            "refresh_token": token_data.get("refresh_token"),
            "expires_in": token_data.get("expires_in"),
            "tenant_id": tenant_id,
            "connected_at": int(time.time()),
        }
        store_tokens_in_db(user_id, teams_token_data, "teams")

        try:
            from services.memory.teams_memory import seed_teams_memory
            seed_teams_memory(user_id)
        except Exception:
            logger.warning("Failed to seed Teams memory (non-fatal)", exc_info=True)

        try:
            from routes.teams.teams_channel_metadata import auto_register_channels_task
            auto_register_channels_task.delay(user_id)
        except Exception:
            logger.warning("Failed to enqueue Teams channel registration (non-fatal)", exc_info=True)

        return redirect(f"{FRONTEND_URL}/teams/manage?teams_auth=success")
    except Exception:
        logger.exception("Error during Teams callback")
        return redirect(_teams_oauth_failure_redirect("unexpected_error"))


def _teams_setup_state(user_id: str, last_bot_message_at: int | None) -> dict:
    # Inbound @mention proves the Teams app, messaging endpoint, and Connect are wired.
    bot_verified = last_bot_message_at is not None
    reply_delivered = _last_bot_reply_at(user_id) is not None
    setup_complete = bot_verified or reply_delivered
    pending: list[dict[str, str]] = []
    if not bot_verified:
        pending.append({
            "id": "install_teams_app",
            "title": "Add Aurora to each Microsoft Team",
            "detail": (
                "In Teams: open the team → Manage team → Apps (or + → Add an app), "
                "install your organization’s Aurora app, then @mention the bot in a channel."
            ),
        })
    return {
        "oauth_connected": True,
        "bot_verified": bot_verified,
        "setup_complete": setup_complete,
        "pending_setup": pending,
    }


def record_teams_bot_reply_delivered(user_id: str) -> None:
    """Mark org setup complete after Aurora posts the first @mention response to Teams."""
    from utils.auth.stateless_auth import store_org_preference

    org_id = get_org_id_for_user(user_id)
    if not org_id:
        return
    store_org_preference(org_id, "teams_last_bot_reply_at", str(int(time.time())))


def _last_bot_reply_at(user_id: str) -> int | None:
    org_id = get_org_id_for_user(user_id)
    if not org_id:
        return None
    raw = get_org_preference(org_id, "teams_last_bot_reply_at")
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _last_bot_message_at(user_id: str) -> int | None:
    org_id = get_org_id_for_user(user_id)
    if not org_id:
        return None
    raw = get_org_preference(org_id, "teams_last_bot_message_at")
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


_MS_JWKS_URL = "https://login.microsoftonline.com/common/discovery/v2.0/keys"
_MS_JWKS_CLIENT: jwt.PyJWKClient | None = None


def _decode_id_token(id_token: str) -> dict:
    """Verify the id_token's signature and audience against Microsoft's signing keys."""
    global _MS_JWKS_CLIENT
    if _MS_JWKS_CLIENT is None:
        _MS_JWKS_CLIENT = jwt.PyJWKClient(_MS_JWKS_URL)
    signing_key = _MS_JWKS_CLIENT.get_signing_key_from_jwt(id_token)
    return jwt.decode(
        id_token,
        signing_key.key,
        algorithms=["RS256"],
        audience=os.getenv("TEAMS_CLIENT_ID", ""),
    )


def _tenant_from_id_token(id_token: str | None) -> str | None:
    """Verified ``tid`` from the id_token, or None so the caller falls back to Graph."""
    if not id_token:
        return None
    try:
        claims = _decode_id_token(id_token)
        tid = str(uuid.UUID(str(claims.get("tid"))))
        # Multi-tenant keys sign for every tenant, so the issuer must name this tid
        if str(claims.get("iss", "")).lower() != f"https://login.microsoftonline.com/{tid}/v2.0":
            return None
        return tid
    except (jwt.PyJWTError, ValueError):
        logger.debug("Could not verify tenant from Teams id_token", exc_info=True)
        return None
    # JWKS fetch failures surface as requests errors, not PyJWTError
    except requests.RequestException:
        logger.debug("Could not fetch Microsoft signing keys", exc_info=True)
        return None


def _tenant_from_graph(access_token: str) -> str | None:
    try:
        resp = requests.get(
            "https://graph.microsoft.com/v1.0/organization",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=20,
        )
        if resp.ok:
            values = resp.json().get("value") or []
            if values:
                return values[0].get("id")
    except Exception:
        logger.debug("Could not resolve tenant from Graph", exc_info=True)
    return None
