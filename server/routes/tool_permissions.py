"""API routes for org tool permissions (action background gate bypass).

Blueprint: tool_permissions_bp
Prefix: /api/org
"""

import logging
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request, set_rls_context
from utils.auth.tool_registry import TOOL_REGISTRY, get_tools_by_connector
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

_ERR_NO_ORG = "No org context"


def _invalidate_permissions_cache(org_id: str) -> None:
    """Increment Redis version so running chats refresh permissions on next tool call."""
    try:
        from utils.cache.redis_client import get_redis_client
        rc = get_redis_client()
        if rc:
            rc.incr(f"tool_perms_version:{org_id}")
    except Exception as e:
        logger.debug("Could not set permissions dirty flag: %s", e)

tool_permissions_bp = Blueprint("tool_permissions", __name__, url_prefix="/api/org")


@tool_permissions_bp.route("/tool-permissions", methods=["GET"])
@require_permission("admin", "access")
def list_permissions(user_id: str):
    """Return registry grouped by connector with current org toggle states."""
    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": _ERR_NO_ORG}), 400

    with db_pool.get_connection() as conn:
        with conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[ToolPerms:list]")
            cur.execute(
                "SELECT tool_key, enabled FROM org_tool_permissions WHERE org_id = %s",
                (org_id,),
            )
            db_state = {row[0]: row[1] for row in cur.fetchall()}

    tools_by_connector = get_tools_by_connector()
    for connector_tools in tools_by_connector.values():
        for tool in connector_tools:
            tool["enabled"] = db_state.get(tool["tool_key"], False)

    return jsonify({
        "tools_by_connector": tools_by_connector,
        "seeded": len(db_state) >= len(TOOL_REGISTRY),
    })


@tool_permissions_bp.route("/tool-permissions/<tool_key>", methods=["PUT"])
@require_permission("admin", "access")
def toggle_permission(user_id: str, tool_key: str):
    """Toggle a single tool on/off."""
    if tool_key not in TOOL_REGISTRY:
        return jsonify({"error": f"Unknown tool_key: {tool_key}"}), 400

    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": _ERR_NO_ORG}), 400

    body = request.get_json(silent=True) or {}
    if "enabled" not in body or not isinstance(body["enabled"], bool):
        return jsonify({"error": "`enabled` must be a boolean"}), 400
    enabled = body["enabled"]

    with db_pool.get_connection() as conn:
        with conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[ToolPerms:toggle]")
            cur.execute(
                """INSERT INTO org_tool_permissions (org_id, tool_key, enabled, updated_by, updated_at)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (org_id, tool_key)
                   DO UPDATE SET enabled = EXCLUDED.enabled,
                                 updated_by = EXCLUDED.updated_by,
                                 updated_at = EXCLUDED.updated_at""",
                (org_id, tool_key, enabled, user_id, datetime.now(timezone.utc)),
            )
            conn.commit()

    _invalidate_permissions_cache(org_id)
    return jsonify({"tool_key": tool_key, "enabled": enabled})


@tool_permissions_bp.route("/tool-permissions/seed", methods=["POST"])
@require_permission("admin", "access")
def seed_defaults(user_id: str):
    """Seed default tool permissions for this org (idempotent)."""
    from utils.auth.tool_registry import seed_org_tool_permissions

    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": _ERR_NO_ORG}), 400

    count = seed_org_tool_permissions(org_id, user_id)

    _invalidate_permissions_cache(org_id)
    return jsonify({"seeded": count})
