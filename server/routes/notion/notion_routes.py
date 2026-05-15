"""Notion connector routes: OAuth + IIT auth, status, disconnect, DB picker."""

import logging
import secrets
from typing import Any, Dict, Optional

import requests
from flask import Blueprint, jsonify, request

from connectors.notion_connector import auth
from connectors.notion_connector.client import (
    NotionAuthExpiredError,
    NotionClient,
    extract_title,
)
from utils.auth.oauth2_state_cache import retrieve_oauth2_state, store_oauth2_state
from utils.auth.rbac_decorators import require_permission
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.log_sanitizer import sanitize
from utils.secrets.secret_ref_utils import delete_user_secret

logger = logging.getLogger(__name__)

notion_bp = Blueprint("notion", __name__)


# ── Helpers ─────────────────────────────────────────────────────────


def _build_oauth_payload(token_data: Dict[str, Any]) -> Dict[str, Any]:
    """Build the credential payload to persist for an OAuth-auth'd workspace."""
    payload: Dict[str, Any] = {
        "access_token": token_data.get("access_token"),
        "type": "oauth",
    }
    for key in (
        "refresh_token",
        "expires_in",
        "expires_at",
        "workspace_id",
        "workspace_name",
        "workspace_icon",
        "bot_id",
        "owner",
    ):
        if token_data.get(key) is not None:
            payload[key] = token_data.get(key)
    return payload


def _handle_oauth_callback(
    user_id: str, code: Optional[str], state: Optional[str]
) -> Any:
    """Shared OAuth code→token exchange path used by /connect and /oauth/callback."""
    if not code:
        return jsonify({"error": "Missing OAuth code"}), 400
    if not state:
        return jsonify({"error": "Missing OAuth state parameter"}), 400

    state_data = retrieve_oauth2_state(state)
    if not state_data:
        return jsonify({"error": "Invalid or expired OAuth state"}), 400
    if (
        state_data.get("user_id") != user_id
        or state_data.get("endpoint") != "notion"
    ):
        logger.warning("[NOTION] OAuth state mismatch for user %s", user_id)
        return jsonify({"error": "OAuth state mismatch"}), 400

    try:
        token_data = auth.exchange_code_for_token(code)
    except Exception as exc:
        logger.error(
            "[NOTION] OAuth token exchange failed for user %s: %s", user_id, exc
        )
        return jsonify({"error": "Notion OAuth token exchange failed"}), 502

    if not token_data.get("access_token"):
        return jsonify(
            {"error": "Notion OAuth token exchange returned no access_token"}
        ), 502

    payload = _build_oauth_payload(token_data)
    try:
        store_tokens_in_db(user_id, payload, "notion")
    except Exception as exc:
        logger.exception("[NOTION] Failed to persist OAuth credentials: %s", exc)
        return jsonify({"error": "Failed to persist Notion credentials"}), 500

    return jsonify(
        {
            "success": True,
            "connected": True,
            "workspaceName": token_data.get("workspace_name"),
            "authType": "oauth",
        }
    )


# ── Routes ──────────────────────────────────────────────────────────


@notion_bp.route("/connect", methods=["POST"])
@require_permission("connectors", "write")
def connect(user_id):
    """Dual-purpose: OAuth handshake (start + callback) and IIT submission."""
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    # ── IIT path ────────────────────────────────────────────────────
    if data.get("token_type") == "iit":
        token = data.get("token")
        if not token:
            return jsonify({"error": "Missing Notion integration token"}), 400

        if not (
            (token.startswith("secret_") or token.startswith("ntn_"))
            and 40 <= len(token) <= 200
        ):
            return jsonify({"error": "Invalid token format"}), 400

        try:
            profile = auth.validate_internal_integration_token(token)
        except requests.HTTPError as exc:
            logger.warning(
                "[NOTION] IIT validation rejected for user %s: %s", user_id, exc
            )
            return jsonify({"error": "Invalid Notion integration token"}), 401
        except Exception as exc:
            logger.warning(
                "[NOTION] IIT validation failed for user %s: %s", user_id, exc
            )
            return jsonify({"error": "Invalid Notion integration token"}), 401

        workspace_name = (
            (profile.get("bot") or {}).get("workspace_name")
            or profile.get("name")
            or "Notion"
        )
        payload = {
            "token": token,
            "type": "iit",
            "workspace_name": workspace_name,
            "bot_id": profile.get("id"),
            "bot_name": profile.get("name"),
        }

        try:
            store_tokens_in_db(user_id, payload, "notion")
        except Exception as exc:
            logger.exception(
                "[NOTION] Failed to persist IIT credentials for user %s: %s",
                user_id,
                exc,
            )
            return jsonify({"error": "Failed to persist Notion credentials"}), 500

        # Invalidate cached status so the UI picks up the new connection
        from utils.cache.redis_client import get_redis_client
        rc = get_redis_client()
        if rc:
            rc.delete(f"notion:status:{user_id}")

        return jsonify(
            {
                "success": True,
                "connected": True,
                "workspaceName": workspace_name,
                "authType": "iit",
            }
        )

    # ── OAuth start ─────────────────────────────────────────────────
    code = data.get("code")
    if not code:
        if not auth.is_oauth_configured():
            return jsonify(
                {"error": "Notion OAuth is not configured on this server"}
            ), 400
        try:
            state = secrets.token_urlsafe(32)
            store_oauth2_state(state, user_id, "notion")
            return jsonify({"authUrl": auth.get_auth_url(state)})
        except Exception as exc:
            logger.exception("[NOTION] Failed to start OAuth flow: %s", exc)
            return jsonify({"error": "Failed to initiate Notion OAuth"}), 500

    # ── OAuth callback exchange ─────────────────────────────────────
    return _handle_oauth_callback(user_id, code, data.get("state"))


@notion_bp.route("/oauth/callback", methods=["POST"])
@require_permission("connectors", "write")
def oauth_callback(user_id):
    """OAuth callback endpoint — frontend exchanges ?code/?state from popup here."""
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    return _handle_oauth_callback(user_id, data.get("code"), data.get("state"))


@notion_bp.route("/status", methods=["GET"])
@require_permission("connectors", "read")
def status(user_id):
    """Check Notion connection status.

    Caches a successful result in Redis for 30s to avoid burning Notion
    API calls on frontend polling.
    """
    from utils.cache.redis_client import get_redis_client
    import json as _json

    oauth_configured = False
    try:
        oauth_configured = auth.is_oauth_configured()
    except Exception as exc:
        logger.debug("[NOTION] is_oauth_configured probe failed: %s", exc)

    # Check cache first
    cache_key = f"notion:status:{user_id}"
    rc = get_redis_client()
    if rc:
        cached = rc.get(cache_key)
        if cached:
            return jsonify(_json.loads(cached))

    try:
        creds = get_token_data(user_id, "notion")
    except Exception as exc:
        logger.error(
            "[NOTION] Failed to retrieve credentials for user %s: %s", user_id, exc
        )
        return jsonify({"connected": False, "oauthConfigured": oauth_configured})

    if not creds:
        return jsonify({"connected": False, "oauthConfigured": oauth_configured})

    try:
        NotionClient(user_id).get_self()
    except NotionAuthExpiredError:
        # Don't cache auth failures — user might reconnect immediately
        return jsonify({
            "connected": False,
            "oauthConfigured": oauth_configured,
            "code": "reauth_required",
            "error": "Notion credentials expired — please reconnect",
        })
    except Exception as exc:
        logger.info(
            "[NOTION] Status validation failed for user %s: %s", user_id, exc
        )
        return jsonify({"connected": False, "oauthConfigured": oauth_configured})

    result = {
        "connected": True,
        "oauthConfigured": oauth_configured,
        "workspaceName": creds.get("workspace_name"),
        "authType": creds.get("type", "oauth"),
    }

    # Cache successful status for 30s
    if rc:
        try:
            rc.setex(cache_key, 30, _json.dumps(result))
        except Exception:
            pass

    return jsonify(result)


@notion_bp.route("/disconnect", methods=["POST", "DELETE"])
@require_permission("connectors", "write")
def disconnect(user_id):
    """Disconnect Notion by removing stored credentials (and revoking OAuth token)."""
    try:
        creds = get_token_data(user_id, "notion")
    except Exception as exc:
        logger.warning(
            "[NOTION] Failed to load credentials before disconnect for user %s: %s",
            user_id,
            exc,
        )
        creds = None

    if creds and (creds.get("type") or "oauth").lower() == "oauth":
        access_token = creds.get("access_token")
        if access_token:
            try:
                auth.revoke_token(access_token)
            except Exception as exc:
                logger.warning(
                    "[NOTION] Best-effort token revoke failed for user %s: %s",
                    user_id,
                    exc,
                )

    try:
        delete_user_secret(user_id, "notion")
    except Exception as exc:
        logger.exception(
            "[NOTION] Failed to delete Notion credentials for user %s: %s",
            user_id,
            exc,
        )
        return jsonify({"error": "Failed to disconnect Notion"}), 500

    # Invalidate cached status so the UI reflects disconnect immediately
    from utils.cache.redis_client import get_redis_client
    rc = get_redis_client()
    if rc:
        rc.delete(f"notion:status:{user_id}")

    logger.info("[NOTION] Disconnected user %s", user_id)
    return jsonify(
        {"success": True, "message": "Notion disconnected successfully"}
    )


@notion_bp.route("/databases", methods=["GET"])
@require_permission("connectors", "read")
def list_databases(user_id):
    """List Notion databases matching an optional search query (for DB picker)."""
    query = (request.args.get("query") or "").strip()
    start_cursor = request.args.get("start_cursor") or None
    try:
        page_size = int(request.args.get("page_size", 100))
    except (TypeError, ValueError):
        page_size = 100
    page_size = max(1, min(page_size, 100))

    try:
        client = NotionClient(user_id)
    except ValueError:
        return jsonify({"error": "Notion not connected"}), 404
    except Exception as exc:
        logger.exception(
            "[NOTION] Failed to initialize client for user %s: %s", user_id, exc
        )
        return jsonify({"error": "Failed to initialize Notion client"}), 500

    try:
        response = client.search_databases(query, max_results=page_size, start_cursor=start_cursor)
    except NotionAuthExpiredError:
        return jsonify(
            {
                "code": "reauth_required",
                "error": "Notion credentials expired — please reconnect",
            }
        ), 401
    except Exception as exc:
        logger.exception(
            "[NOTION] search_databases failed for user %s: %s", user_id, exc
        )
        return jsonify({"error": "Failed to list Notion databases"}), 502

    results: list[Dict[str, Any]] = []
    for item in response.get("results", []) or []:
        title = extract_title(item)
        description_parts = item.get("description") or []
        description: Optional[str] = None
        if description_parts and isinstance(description_parts, list):
            first_desc = description_parts[0] or {}
            description = first_desc.get("plain_text") or None

        entry: Dict[str, Any] = {
            "id": item.get("id"),
            "title": title,
            "url": item.get("url"),
            "icon": item.get("icon"),
        }
        if description:
            entry["description"] = description
        results.append(entry)

    payload: Dict[str, Any] = {"databases": results}
    next_cursor = response.get("next_cursor")
    if next_cursor:
        payload["next_cursor"] = next_cursor
    return jsonify(payload)


@notion_bp.route("/databases/<db_id>", methods=["GET"])
@require_permission("connectors", "read")
def get_database(user_id, db_id: str):
    """Return a shallow summary of a Notion database (for property-mapping UI)."""
    if not db_id:
        return jsonify({"error": "Database id is required"}), 400

    try:
        client = NotionClient(user_id)
    except ValueError:
        return jsonify({"error": "Notion not connected"}), 404
    except Exception as exc:
        logger.exception(
            "[NOTION] Failed to initialize client for user %s: %s", user_id, exc
        )
        return jsonify({"error": "Failed to initialize Notion client"}), 500

    try:
        database = client.get_database(db_id)
    except NotionAuthExpiredError:
        return jsonify(
            {
                "code": "reauth_required",
                "error": "Notion credentials expired — please reconnect",
            }
        ), 401
    except Exception as exc:
        logger.exception(
            "[NOTION] get_database failed for user %s (db=%s): %s",
            sanitize(user_id),
            sanitize(db_id),
            exc,
        )
        return jsonify({"error": "Failed to fetch Notion database"}), 502

    title = extract_title(database)

    raw_properties = database.get("properties") or {}
    title_property: Optional[str] = None
    properties_summary: Dict[str, Dict[str, Any]] = {}
    for name, prop in raw_properties.items():
        if not isinstance(prop, dict):
            continue
        prop_type = prop.get("type")
        properties_summary[name] = {
            "type": prop_type,
            "id": prop.get("id"),
        }
        if title_property is None and prop_type == "title":
            title_property = name

    return jsonify(
        {
            "id": database.get("id"),
            "title": title,
            "titleProperty": title_property,
            "properties": properties_summary,
        }
    )
