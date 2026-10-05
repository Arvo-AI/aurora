"""Routes for registering customer-supplied MCP servers as a connector.

A server is only ever stored after a successful handshake, so a bad URL or
token surfaces at registration instead of mid-incident.
"""

from __future__ import annotations

import asyncio
import logging
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
from connectors.mcp_connector.store import (
    MAX_SERVERS,
    list_servers,
    find_server,
    is_read_tool,
    remove_server,
    server_summary,
    slugify_label,
    upsert_server,
)
from utils.auth.rbac_decorators import require_permission
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

mcp_bp = Blueprint("mcp_servers", __name__)

MAX_URL_CHARS = 2048
MAX_TOKEN_CHARS = 4096
MAX_HEADER_NAME_CHARS = 64


def _parse_auth(data: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    """Build the auth config from a request body. Returns (auth, error)."""
    auth_type = str(data.get("authType") or data.get("auth_type") or "none").lower()
    if auth_type not in AUTH_TYPES:
        return {}, f"authType must be one of: {', '.join(AUTH_TYPES)}"
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


def _probe_sync(url: str, auth: Dict[str, Any], transport: str):
    """Run the async probe from Flask's synchronous request context."""
    return asyncio.run(probe(url, auth, transport))


def _register(user_id: str, data: Dict[str, Any], label: str) -> Tuple[Any, int]:
    """Validate, probe, and store one server. Returns (flask_response, status)."""
    url = str(data.get("url") or "").strip()
    if not url or len(url) > MAX_URL_CHARS:
        return jsonify({"error": "url is required"}), 400

    transport = str(data.get("transport") or "streamable_http").lower()
    if transport not in TRANSPORTS:
        return jsonify({"error": f"transport must be one of: {', '.join(TRANSPORTS)}"}), 400

    auth, auth_error = _parse_auth(data)
    if auth_error:
        return jsonify({"error": auth_error}), 400

    read_only = data.get("readOnly", data.get("read_only", True))
    allow_in_background = [
        str(t) for t in (data.get("allowInBackground") or data.get("allow_in_background") or [])
    ]

    try:
        tools, transport_used = _probe_sync(url, auth, transport)
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

    if bool(read_only):
        tools = [t for t in tools if is_read_tool(t.get("name", ""))]
        if not tools:
            return jsonify({
                "error": "This server exposes no read-only tools. Uncheck read-only to "
                         "register its write tools (they will require confirmation)."
            }), 400

    # Cap after filtering so a server's reads are never hidden behind its writes.
    truncated = len(tools) > MAX_TOOLS_PER_SERVER
    tools = tools[:MAX_TOOLS_PER_SERVER]

    server = {
        "label": label,
        "url": url,
        "transport": transport_used,
        "auth": auth,
        "read_only": bool(read_only),
        "allow_in_background": allow_in_background,
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

    # Re-probe with the stored credentials; the body may override nothing.
    data = {
        "url": existing.get("url"),
        "transport": existing.get("transport"),
        "authType": (existing.get("auth") or {}).get("type", "none"),
        "token": (existing.get("auth") or {}).get("token"),
        "headerName": (existing.get("auth") or {}).get("header_name"),
        "readOnly": existing.get("read_only", True),
        "allowInBackground": existing.get("allow_in_background") or [],
    }
    return _register(user_id, data, existing["label"])


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
