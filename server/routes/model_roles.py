"""API routes for per-org model role bindings (orchestrator/subagent/triage/judge)."""

import logging

from flask import Blueprint, jsonify, request

from utils.auth.rbac_decorators import require_permission, require_auth_only
from utils.auth.stateless_auth import (
    get_org_id_from_request,
    set_rls_context,
)
from chat.backend.agent.llm import ModelConfig
from chat.backend.agent.model_mapper import ModelMapper

logger = logging.getLogger(__name__)

model_roles_bp = Blueprint("model_roles", __name__, url_prefix="/api/settings")

VALID_ROLES = ("orchestrator", "subagent", "triage", "judge")


def _default_bindings() -> dict:
    provider, model_id = ModelMapper.split_provider_model(ModelConfig.MAIN_MODEL)
    return {
        role: {"provider": provider or "", "model_id": model_id}
        for role in VALID_ROLES
    }


def _fetch_bindings(user_id: str, org_id: str, log_prefix: str) -> list:
    from utils.db.connection_pool import db_pool
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix=log_prefix)
            cur.execute(
                "SELECT role, provider, model_id, updated_at "
                "FROM model_roles WHERE org_id = %s",
                (org_id,),
            )
            rows = cur.fetchall()

    by_role = {
        r[0]: {
            "role": r[0],
            "provider": r[1],
            "model_id": r[2],
            "updated_at": r[3].isoformat() if r[3] else None,
        }
        for r in rows
    }

    defaults = _default_bindings()
    result = []
    for role in VALID_ROLES:
        if role in by_role:
            result.append(by_role[role])
        else:
            result.append({
                "role": role,
                "provider": defaults[role]["provider"],
                "model_id": defaults[role]["model_id"],
                "updated_at": None,
            })
    return result


@model_roles_bp.route("/model-roles", methods=["GET"])
@require_auth_only
def list_model_roles(user_id):
    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No organization context"}), 403

    bindings = _fetch_bindings(user_id, org_id, "[ModelRoles:list]")
    return jsonify({"bindings": bindings})


@model_roles_bp.route("/model-roles", methods=["PUT"])
@require_permission("admin", "access")
def upsert_model_roles(user_id):
    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No organization context"}), 403

    data = request.get_json() or {}
    bindings = data.get("bindings")
    if not isinstance(bindings, list) or not bindings:
        return jsonify({"error": "bindings must be a non-empty list"}), 400

    validated = []
    for entry in bindings:
        if not isinstance(entry, dict):
            return jsonify({"error": "each binding must be an object"}), 400
        role = entry.get("role")
        provider = entry.get("provider")
        model_id = entry.get("model_id")
        if role not in VALID_ROLES:
            return jsonify({"error": f"invalid role: {role}"}), 400
        if not isinstance(provider, str) or not provider.strip() or len(provider) > 64:
            return jsonify({"error": "provider must be a non-empty string <= 64 chars"}), 400
        if not isinstance(model_id, str) or not model_id.strip() or len(model_id) > 255:
            return jsonify({"error": "model_id must be a non-empty string <= 255 chars"}), 400
        validated.append((role, provider.strip(), model_id.strip()))

    from utils.db.connection_pool import db_pool
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[ModelRoles:upsert]")
            for role, provider, model_id in validated:
                cur.execute(
                    "INSERT INTO model_roles (org_id, role, provider, model_id, updated_at) "
                    "VALUES (%s, %s, %s, %s, NOW()) "
                    "ON CONFLICT (org_id, role) DO UPDATE "
                    "SET provider = EXCLUDED.provider, model_id = EXCLUDED.model_id, updated_at = NOW()",
                    (org_id, role, provider, model_id),
                )
        conn.commit()

    bindings_out = _fetch_bindings(user_id, org_id, "[ModelRoles:upsert:read]")
    return jsonify({"bindings": bindings_out})


@model_roles_bp.route("/model-roles/<role>", methods=["DELETE"])
@require_permission("admin", "access")
def delete_model_role(user_id, role):
    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No organization context"}), 403

    if role not in VALID_ROLES:
        return jsonify({"error": f"invalid role: {role}"}), 400

    from utils.db.connection_pool import db_pool
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[ModelRoles:delete]")
            cur.execute(
                "DELETE FROM model_roles WHERE org_id = %s AND role = %s",
                (org_id, role),
            )
        conn.commit()

    return jsonify({"role": role, "deleted": True})
