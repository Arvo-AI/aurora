"""Routes for registering customer-supplied MCP servers as a connector.

A server is only ever stored after a successful handshake, so a bad URL or
token surfaces at registration instead of mid-incident.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, Tuple

from flask import Blueprint, jsonify, request

from connectors.mcp_connector.client import (
    AUTH_TYPES,
    MAX_TOOLS_PER_SERVER,
    TRANSPORTS,
    MCPAuthError,
    MCPConnectionError,
    probe,
)
from connectors.mcp_connector.oauth import (
    AuthServer,
    OAuthDiscoveryError,
    OAuthRegistrationUnsupported,
    build_authorize_url,
    discover,
    exchange_code,
    register_client,
)
from connectors.mcp_connector.store import (
    MAX_SERVERS,
    list_servers,
    find_server,
    is_read_tool,
    remove_server,
    server_summary,
    set_tool_mode,
    slugify_label,
    upsert_server,
)
from utils.auth.oauth2_state_cache import retrieve_oauth2_state, store_oauth2_state
from utils.auth.rbac_decorators import require_permission
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

mcp_bp = Blueprint("mcp_servers", __name__)

MAX_URL_CHARS = 2048
MAX_TOKEN_CHARS = 4096
MAX_HEADER_NAME_CHARS = 64

# Namespaces this connector's OAuth state so a state minted for Notion or Jira
# cannot be redeemed here.
_OAUTH_ENDPOINT = "mcp_server"


def _redirect_uri() -> str:
    """The fixed callback URL registered with every provider.

    Always derived from server config, never reflected from the request: a
    attacker-supplied redirect_uri is the classic way to steal an auth code.
    """
    frontend = (os.getenv("FRONTEND_URL") or "").rstrip("/")
    return f"{frontend}/mcp/callback"


def _parse_auth(data: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    """Build the auth config from a request body. Returns (auth, error)."""
    auth_type = str(data.get("authType") or data.get("auth_type") or "none").lower()
    if auth_type not in AUTH_TYPES:
        return {}, f"authType must be one of: {', '.join(AUTH_TYPES)}"
    if auth_type == "oauth":
        # There is no token to accept here -- OAuth servers go through
        # /oauth/start so the user can consent in a browser.
        return {}, "Use the OAuth flow to connect this server"
    if auth_type == "none":
        return {"type": "none"}, ""

    token = data.get("token") or ""
    if not isinstance(token, str) or not token.strip():
        return {}, "token is required for this authType"
    if len(token) > MAX_TOKEN_CHARS:
        return {}, "token is too long"
    auth: Dict[str, Any] = {"type": auth_type, "token": token.strip()}

    if auth_type == "header":
        name = data.get("headerName") or data.get("header_name") or ""
        if not isinstance(name, str) or not name.strip():
            return {}, "headerName is required when authType is 'header'"
        if len(name) > MAX_HEADER_NAME_CHARS or not name.replace("-", "").isalnum():
            return {}, "headerName must be alphanumeric with dashes"
        auth["header_name"] = name.strip()
    return auth, ""


def _probe_sync(url: str, auth: Dict[str, Any], transport: str, on_refresh=None):
    """Run the async probe from Flask's synchronous request context."""
    return asyncio.run(probe(url, auth, transport, on_refresh))


def _register(user_id: str, data: Dict[str, Any], label: str) -> Tuple[Any, int]:
    """Validate, probe, and store one server. Returns (flask_response, status)."""
    url = str(data.get("url") or "").strip()
    if not url or len(url) > MAX_URL_CHARS:
        return jsonify({"error": "url is required"}), 400

    transport = str(data.get("transport") or "streamable_http").lower()
    if transport not in TRANSPORTS:
        return jsonify({"error": f"transport must be one of: {', '.join(TRANSPORTS)}"}), 400

    # ``_auth`` is set by the OAuth flow, which has already built the blob.
    auth = data.get("_auth")
    if not auth:
        auth, auth_error = _parse_auth(data)
        if auth_error:
            return jsonify({"error": auth_error}), 400

    read_only = data.get("readOnly", data.get("read_only", True))
    allow_in_background = [
        str(t) for t in (data.get("allowInBackground") or data.get("allow_in_background") or [])
    ]
    # Carried through rather than rebuilt: a refresh re-probes the server, and
    # dropping these would silently reset every per-tool override the user set.
    tool_modes = data.get("toolModes") or data.get("tool_modes") or {}

    # The probe can refresh an OAuth token; keep whichever blob we end up with
    # so the stored credentials are the ones that actually worked.
    final_auth: Dict[str, Any] = dict(auth)

    def _capture(new_auth: Dict[str, Any]) -> None:
        final_auth.clear()
        final_auth.update(new_auth)

    try:
        tools, transport_used = _probe_sync(url, auth, transport, _capture)
    except ValueError as exc:  # SSRF / malformed URL — message is user-facing
        logger.warning("[MCP] Rejected target for user %s: %s", sanitize(user_id), sanitize(exc))
        return jsonify({"error": str(exc)}), 400
    except MCPAuthError as exc:
        return jsonify({"error": str(exc)}), 400
    except MCPConnectionError as exc:
        logger.warning("[MCP] Probe failed for user %s: %s", sanitize(user_id), sanitize(exc))
        return jsonify({"error": f"Could not reach the MCP server: {exc}"}), 502
    except Exception:
        logger.exception("[MCP] Unexpected probe failure for user %s", sanitize(user_id))
        return jsonify({"error": "Failed to connect to the MCP server"}), 502

    # Every tool is stored regardless of read_only, which is applied as a filter
    # at agent-build time instead. Dropping tools here made the switch
    # destructive: flipping it later required re-registering the server, and the
    # cached list no longer described what the server actually offers.
    if bool(read_only) and not any(is_read_tool(t) for t in tools):
        return jsonify({
            "error": "This server exposes no read-only tools. Uncheck read-only to "
                     "register its write tools (they will require confirmation)."
        }), 400

    truncated = len(tools) > MAX_TOOLS_PER_SERVER
    if truncated:
        # Keep reads first so a cap can never strand the tools the agent is
        # most likely to need (GitHub's MCP exposes 49).
        tools = sorted(tools, key=lambda t: not is_read_tool(t))[:MAX_TOOLS_PER_SERVER]

    server = {
        "label": label,
        "url": url,
        "transport": transport_used,
        "auth": final_auth,
        "read_only": bool(read_only),
        "allow_in_background": allow_in_background,
        "tool_modes": tool_modes,
        "tools": tools,
        "validated_at": datetime.now(timezone.utc).isoformat(),
    }

    ok, error = upsert_server(user_id, server)
    if not ok:
        return jsonify({"error": error}), 400

    logger.info(
        "[MCP] Registered server '%s' for user %s (%d tools, transport=%s)",
        sanitize(label), sanitize(user_id), len(tools), transport_used,
    )
    return jsonify({
        "success": True,
        "server": server_summary(server),
        **({"warning": f"Only the first {MAX_TOOLS_PER_SERVER} tools were registered."}
           if truncated else {}),
    }), 200


@mcp_bp.route("/servers/oauth/start", methods=["POST"])
@require_permission("connectors", "write")
def oauth_start(user_id):
    """Discover the server's OAuth provider, register Aurora, return a consent URL.

    Nothing is persisted as a connected server here -- only the short-lived
    flow state. The server is stored in ``oauth_complete`` after a handshake.
    """
    data = request.get_json(silent=True) or {}
    label = slugify_label(data.get("label"))
    if not label:
        return jsonify({"error": "label is required (letters, digits and dashes)"}), 400
    if find_server(user_id, label):
        return jsonify({"error": f"A server labelled '{label}' already exists"}), 409

    url = str(data.get("url") or "").strip()
    if not url or len(url) > MAX_URL_CHARS:
        return jsonify({"error": "url is required"}), 400

    transport = str(data.get("transport") or "streamable_http").lower()
    if transport not in TRANSPORTS:
        return jsonify({"error": f"transport must be one of: {', '.join(TRANSPORTS)}"}), 400

    try:
        auth_server = asyncio.run(discover(url))
        client_id = str(data.get("clientId") or "").strip()
        client_secret = str(data.get("clientSecret") or "").strip() or None
        if not client_id:
            creds = asyncio.run(register_client(auth_server, _redirect_uri()))
            client_id, client_secret = creds["client_id"], creds.get("client_secret")
    except ValueError as exc:  # SSRF guard on a discovered endpoint
        logger.warning("[MCP] OAuth target rejected for %s: %s", sanitize(user_id), sanitize(exc))
        return jsonify({"error": str(exc)}), 400
    except OAuthRegistrationUnsupported as exc:
        return jsonify({"error": str(exc), "needsClientId": True}), 400
    except OAuthDiscoveryError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        logger.exception("[MCP] OAuth discovery failed for user %s", sanitize(user_id))
        return jsonify({"error": "Could not start the OAuth flow for this server"}), 502

    state = secrets.token_urlsafe(32)
    authorize_url, verifier = build_authorize_url(
        auth_server, client_id, _redirect_uri(), state, url
    )
    # The verifier never leaves Aurora; the cache ties it to this user + flow.
    store_oauth2_state(
        state,
        user_id,
        _OAUTH_ENDPOINT,
        project_id=json.dumps({
            "label": label, "url": url, "transport": transport,
            "read_only": bool(data.get("readOnly", data.get("read_only", True))),
            "client_id": client_id, "client_secret": client_secret,
            "token_endpoint": auth_server.token_endpoint,
        }),
        code_verifier=verifier,
    )
    logger.info("[MCP] OAuth flow started for '%s' (user %s)", sanitize(label), sanitize(user_id))
    return jsonify({"authorizeUrl": authorize_url, "state": state})


@mcp_bp.route("/servers/oauth/complete", methods=["POST"])
@require_permission("connectors", "write")
def oauth_complete(user_id):
    """Redeem the authorization code, handshake, and store the server."""
    data = request.get_json(silent=True) or {}
    code, state = data.get("code"), data.get("state")
    if not code or not state:
        return jsonify({"error": "code and state are required"}), 400

    flow = retrieve_oauth2_state(state)
    if not flow:
        return jsonify({"error": "Invalid or expired OAuth state"}), 400
    # Both checks matter: user_id stops one user redeeming another's code, and
    # endpoint stops a state minted for a different connector being replayed here.
    if flow.get("user_id") != user_id or flow.get("endpoint") != _OAUTH_ENDPOINT:
        logger.warning("[MCP] OAuth state mismatch for user %s", sanitize(user_id))
        return jsonify({"error": "OAuth state mismatch"}), 400

    verifier = flow.get("code_verifier")
    try:
        meta = json.loads(flow.get("project_id") or "{}")
    except ValueError:
        meta = {}
    label, url = meta.get("label"), meta.get("url")
    if not (verifier and label and url):
        return jsonify({"error": "OAuth state is incomplete; start again"}), 400

    try:
        auth = asyncio.run(exchange_code(
            AuthServer(
                issuer=url,
                authorization_endpoint="",  # unused on the token leg
                token_endpoint=meta["token_endpoint"],
                registration_endpoint=None,
                scopes_supported=(),
            ),
            meta["client_id"], meta.get("client_secret"),
            code, verifier, _redirect_uri(), url,
        ))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except OAuthDiscoveryError as exc:
        return jsonify({"error": f"Could not complete authorization: {exc}"}), 400
    except Exception:
        logger.exception("[MCP] Token exchange failed for user %s", sanitize(user_id))
        return jsonify({"error": "Could not complete the OAuth flow"}), 502

    return _register(user_id, {
        "url": url,
        "transport": meta.get("transport", "streamable_http"),
        "readOnly": meta.get("read_only", True),
        "_auth": auth,
    }, label)


@mcp_bp.route("/servers", methods=["GET"])
@require_permission("connectors", "read")
def get_servers(user_id):
    """List registered MCP servers, without credentials."""
    servers = list_servers(user_id)
    return jsonify({
        "servers": [server_summary(s) for s in servers],
        "maxServers": MAX_SERVERS,
        "maxToolsPerServer": MAX_TOOLS_PER_SERVER,
    })


@mcp_bp.route("/servers", methods=["POST"])
@require_permission("connectors", "write")
def create_server(user_id):
    """Register a new MCP server after a successful handshake."""
    data = request.get_json(silent=True) or {}
    label = slugify_label(data.get("label"))
    if not label:
        return jsonify({"error": "label is required (letters, digits and dashes)"}), 400
    if find_server(user_id, label):
        return jsonify({"error": f"A server labelled '{label}' already exists"}), 409
    return _register(user_id, data, label)


@mcp_bp.route("/servers/<label>/refresh", methods=["POST"])
@require_permission("connectors", "write")
def refresh_server(user_id, label):
    """Re-probe a stored server and update its cached tool list."""
    existing = find_server(user_id, label)
    if not existing:
        return jsonify({"error": "Unknown MCP server"}), 404

    auth = existing.get("auth") or {}
    return _register(user_id, {
        "url": existing.get("url"),
        "transport": existing.get("transport"),
        "readOnly": existing.get("read_only", True),
        "allowInBackground": existing.get("allow_in_background") or [],
        "toolModes": existing.get("tool_modes") or {},
        # Reuse the stored credentials verbatim rather than re-deriving them:
        # an OAuth blob cannot be rebuilt from form fields.
        "_auth": auth,
    }, existing["label"])


@mcp_bp.route("/servers/<label>/tools/<tool_name>", methods=["PATCH"])
@require_permission("connectors", "write")
def patch_tool_mode(user_id, label, tool_name):
    """Set one tool's override to auto, always or never.

    Scoped to a single tool so the caller never has to round-trip the whole
    server definition (and its cached tool list) to change one setting.
    """
    mode = str((request.get_json(silent=True) or {}).get("mode", ""))
    ok, error = set_tool_mode(user_id, label, tool_name, mode)
    if not ok:
        return jsonify({"error": error}), 404 if "Unknown" in error else 400
    logger.info(
        "[MCP] Tool '%s' on '%s' set to '%s' for user %s",
        sanitize(tool_name), sanitize(label), sanitize(mode), sanitize(user_id),
    )
    return jsonify({"success": True, "mode": mode})


@mcp_bp.route("/servers/<label>", methods=["DELETE"])
@require_permission("connectors", "write")
def delete_server(user_id, label):
    """Remove one server; removing the last one deletes the stored secret."""
    if not remove_server(user_id, label):
        return jsonify({"error": "Unknown MCP server"}), 404
    logger.info("[MCP] Removed server '%s' for user %s", sanitize(label), sanitize(user_id))
    return jsonify({"success": True, "remaining": len(list_servers(user_id))})


@mcp_bp.route("/status", methods=["GET"])
@require_permission("connectors", "read")
def status(user_id):
    """Connection status: connected when at least one server is registered."""
    servers = list_servers(user_id)
    return jsonify({
        "connected": bool(servers),
        "serverCount": len(servers),
        "toolCount": sum(len(s.get("tools") or []) for s in servers),
        "servers": [server_summary(s) for s in servers],
    })
