"""API routes for per-org sub-agent enable/disable overrides."""

import logging

from flask import Blueprint, jsonify, request

from utils.auth.rbac_decorators import require_permission, require_auth_only
from utils.auth.stateless_auth import (
    get_org_id_from_request,
    set_rls_context,
)
from chat.backend.agent.orchestrator.catalog import BUILTIN_CATALOG

logger = logging.getLogger(__name__)

subagent_overrides_bp = Blueprint("subagent_overrides", __name__, url_prefix="/api/settings")


@subagent_overrides_bp.route("/sub-agents", methods=["GET"])
@require_auth_only
def list_subagents(user_id):
    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No organization context"}), 403

    from utils.db.connection_pool import db_pool
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[SubAgentOverrides:list]")
            cur.execute(
                "SELECT subagent_id FROM subagent_overrides "
                "WHERE org_id = %s AND enabled = FALSE",
                (org_id,),
            )
            disabled_ids = {r[0] for r in cur.fetchall()}

    subagents = []
    for sid, entry in BUILTIN_CATALOG.items():
        subagents.append({
            "id": sid,
            "ui_label": entry.get("ui_label"),
            "domain": entry.get("domain"),
            "enabled": sid not in disabled_ids,
        })

    return jsonify({"subagents": subagents})


@subagent_overrides_bp.route("/sub-agents/<subagent_id>", methods=["PUT"])
@require_permission("admin", "access")
def update_subagent(user_id, subagent_id):
    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No organization context"}), 403

    if subagent_id not in BUILTIN_CATALOG:
        return jsonify({"error": "Unknown sub-agent"}), 404

    data = request.get_json() or {}
    enabled = data.get("enabled")
    if not isinstance(enabled, bool):
        return jsonify({"error": "enabled must be a boolean"}), 400

    from utils.db.connection_pool import db_pool
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[SubAgentOverrides:update]")
            cur.execute(
                "INSERT INTO subagent_overrides (org_id, subagent_id, enabled, updated_at) "
                "VALUES (%s, %s, %s, NOW()) "
                "ON CONFLICT (org_id, subagent_id) DO UPDATE "
                "SET enabled = EXCLUDED.enabled, updated_at = NOW()",
                (org_id, subagent_id, enabled),
            )
        conn.commit()

    return jsonify({"id": subagent_id, "enabled": enabled})
