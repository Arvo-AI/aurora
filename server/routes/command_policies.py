"""API routes for command policy management (allow/deny firewall rules).

Blueprint: command_policies_bp
Prefix: /api/org
"""

import json
import logging
from datetime import datetime

from flask import Blueprint, jsonify, request

from utils.auth.rbac_decorators import require_permission, require_auth_only
from utils.auth.stateless_auth import (
    get_org_id_from_request,
    get_user_preference,
    store_user_preference,
)
from utils.auth.command_policy import (
    evaluate_compound_command,
    get_policy_templates,
    get_seed_rules,
    invalidate_cache,
    validate_pattern,
)

logger = logging.getLogger(__name__)

command_policies_bp = Blueprint("command_policies", __name__, url_prefix="/api/org")


def _list_states(org_id: str) -> dict:
    """Read allowlist/denylist toggle states from user_preferences."""
    org_key = f"__org__{org_id}"
    al = get_user_preference(org_key, "command_policy_allowlist") or "off"
    dl = get_user_preference(org_key, "command_policy_denylist") or "off"
    at = get_user_preference(org_key, "command_policy_active_template")
    return {
        "allowlist_enabled": str(al).lower() == "on",
        "denylist_enabled": str(dl).lower() == "on",
        "active_template_id": at or None,
    }


@command_policies_bp.route("/command-policies", methods=["GET", "OPTIONS"])
@require_auth_only
def list_policies(user_id):
    if request.method == "OPTIONS":
        return jsonify({}), 200

    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No organization context"}), 403

    from utils.db.connection_pool import db_pool
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, mode, pattern, description, priority, enabled, "
                "created_at, updated_at, updated_by, source "
                "FROM org_command_policies WHERE org_id = %s ORDER BY priority DESC",
                (org_id,),
            )
            rows = cur.fetchall()

    allow_rules = []
    deny_rules = []
    for r in rows:
        rule = {
            "id": r[0], "mode": r[1], "pattern": r[2],
            "description": r[3] or "", "priority": r[4],
            "enabled": r[5],
            "created_at": r[6].isoformat() if r[6] else None,
            "updated_at": r[7].isoformat() if r[7] else None,
            "updated_by": r[8],
            "source": r[9] or "custom",
        }
        (allow_rules if r[1] == "allow" else deny_rules).append(rule)

    states = _list_states(org_id)
    return jsonify({
        "allow_rules": allow_rules,
        "deny_rules": deny_rules,
        **states,
    })


@command_policies_bp.route("/command-policies", methods=["POST"])
@require_permission("admin", "access")
def create_policy(user_id):
    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No organization context"}), 403

    data = request.get_json() or {}
    mode = data.get("mode")
    pattern = data.get("pattern", "").strip()
    description = data.get("description", "").strip()
    try:
        priority = int(data.get("priority", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "priority must be an integer"}), 400

    if mode not in ("allow", "deny"):
        return jsonify({"error": "mode must be 'allow' or 'deny'"}), 400
    if not pattern:
        return jsonify({"error": "pattern is required"}), 400

    err = validate_pattern(pattern)
    if err:
        return jsonify({"error": "Invalid regex pattern"}), 400

    from utils.db.connection_pool import db_pool
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO org_command_policies "
                    "(org_id, mode, pattern, description, priority, updated_by) "
                    "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                    (org_id, mode, pattern, description, priority, user_id),
                )
                new_id = cur.fetchone()[0]
            conn.commit()
    except Exception as e:
        if "unique" in str(e).lower() or "duplicate" in str(e).lower():
            return jsonify({"error": "A rule with this mode and pattern already exists"}), 409
        raise

    invalidate_cache(org_id)
    return jsonify({"id": new_id, "status": "created"}), 201


@command_policies_bp.route("/command-policies/<int:rule_id>", methods=["PUT", "OPTIONS"])
@require_permission("admin", "access")
def update_policy(user_id, rule_id):
    if request.method == "OPTIONS":
        return jsonify({}), 200

    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No organization context"}), 403

    data = request.get_json() or {}
    updates = []
    params = []

    for field, col in [("mode", "mode"), ("pattern", "pattern"),
                       ("description", "description"), ("priority", "priority"),
                       ("enabled", "enabled")]:
        if field in data:
            if field == "mode" and data[field] not in ("allow", "deny"):
                return jsonify({"error": "mode must be 'allow' or 'deny'"}), 400
            if field == "pattern":
                err = validate_pattern(data[field])
                if err:
                    return jsonify({"error": "Invalid regex pattern"}), 400
            updates.append(f"{col} = %s")
            params.append(data[field])

    if not updates:
        return jsonify({"error": "No fields to update"}), 400

    updates.append("updated_at = %s")
    params.append(datetime.utcnow())
    updates.append("updated_by = %s")
    params.append(user_id)
    params.extend([rule_id, org_id])

    from utils.db.connection_pool import db_pool
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE org_command_policies SET {', '.join(updates)} "
                "WHERE id = %s AND org_id = %s",
                params,
            )
            if cur.rowcount == 0:
                return jsonify({"error": "Rule not found"}), 404
        conn.commit()

    invalidate_cache(org_id)
    store_user_preference(f"__org__{org_id}", "command_policy_active_template", None)
    return jsonify({"status": "updated"})


@command_policies_bp.route("/command-policies/<int:rule_id>", methods=["DELETE"])
@require_permission("admin", "access")
def delete_policy(user_id, rule_id):
    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No organization context"}), 403

    from utils.db.connection_pool import db_pool
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM org_command_policies WHERE id = %s AND org_id = %s",
                (rule_id, org_id),
            )
            if cur.rowcount == 0:
                return jsonify({"error": "Rule not found"}), 404
        conn.commit()

    invalidate_cache(org_id)
    store_user_preference(f"__org__{org_id}", "command_policy_active_template", None)
    return jsonify({"status": "deleted"})


@command_policies_bp.route("/command-policies/test", methods=["POST", "OPTIONS"])
@require_auth_only
def test_command(user_id):
    if request.method == "OPTIONS":
        return jsonify({}), 200

    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No organization context"}), 403

    data = request.get_json() or {}
    command = data.get("command", "").strip()
    if not command:
        return jsonify({"error": "command is required"}), 400

    verdict = evaluate_compound_command(org_id, command)
    return jsonify({
        "allowed": verdict.allowed,
        "rule_description": verdict.rule_description,
        "command": command,
    })


@command_policies_bp.route("/command-policy-toggle", methods=["PUT", "OPTIONS"])
@require_permission("admin", "access")
def toggle_list(user_id):
    if request.method == "OPTIONS":
        return jsonify({}), 200

    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No organization context"}), 403

    data = request.get_json() or {}
    list_name = data.get("list")
    enabled = data.get("enabled")

    if list_name not in ("allowlist", "denylist"):
        return jsonify({"error": "list must be 'allowlist' or 'denylist'"}), 400
    if not isinstance(enabled, bool):
        return jsonify({"error": "enabled must be a boolean"}), 400

    pref_key = f"command_policy_{list_name}"
    org_key = f"__org__{org_id}"
    store_user_preference(org_key, pref_key, "on" if enabled else "off")

    # Auto-seed rules on first enable if list is empty
    if enabled:
        mode = "allow" if list_name == "allowlist" else "deny"
        from utils.db.connection_pool import db_pool
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM org_command_policies "
                    "WHERE org_id = %s AND mode = %s",
                    (org_id, mode),
                )
                count = cur.fetchone()[0]

                if count == 0:
                    seeds = get_seed_rules().get(mode, [])
                    for seed in seeds:
                        cur.execute(
                            "INSERT INTO org_command_policies "
                            "(org_id, mode, pattern, description, priority, updated_by) "
                            "VALUES (%s, %s, %s, %s, %s, %s)",
                            (org_id, mode, seed["pattern"],
                             seed["description"], seed["priority"], user_id),
                        )
            conn.commit()

    invalidate_cache(org_id)
    return jsonify({"status": "updated", **_list_states(org_id)})


# ---------------------------------------------------------------------------
# Template library endpoints
# ---------------------------------------------------------------------------

@command_policies_bp.route("/command-policy-templates", methods=["GET", "OPTIONS"])
@require_auth_only
def list_templates(user_id):
    if request.method == "OPTIONS":
        return jsonify({}), 200

    templates = get_policy_templates()
    result = []
    for tpl in templates:
        result.append({
            "id": tpl["id"],
            "name": tpl["name"],
            "description": tpl["description"],
            "allow_count": len(tpl["allow"]),
            "deny_count": len(tpl["deny"]),
            "allow": tpl["allow"],
            "deny": tpl["deny"],
        })
    return jsonify(result)


@command_policies_bp.route("/command-policy-templates/apply", methods=["POST", "OPTIONS"])
@require_permission("admin", "access")
def apply_template(user_id):
    if request.method == "OPTIONS":
        return jsonify({}), 200

    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No organization context"}), 403

    data = request.get_json() or {}
    template_id = data.get("template_id")
    if not template_id:
        return jsonify({"error": "template_id is required"}), 400

    templates = {t["id"]: t for t in get_policy_templates()}
    tpl = templates.get(template_id)
    if not tpl:
        return jsonify({"error": f"Unknown template: {template_id}"}), 400

    org_key = f"__org__{org_id}"
    pref_upsert = (
        "INSERT INTO user_preferences (user_id, org_id, preference_key, preference_value) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (user_id, org_id, preference_key) WHERE org_id IS NOT NULL DO UPDATE "
        "SET preference_value = EXCLUDED.preference_value, updated_at = CURRENT_TIMESTAMP"
    )
    from utils.db.connection_pool import db_pool
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM org_command_policies WHERE org_id = %s AND source = 'template'",
                (org_id,),
            )
            for mode_key in ("allow", "deny"):
                for rule in tpl[mode_key]:
                    cur.execute(
                        "INSERT INTO org_command_policies "
                        "(org_id, mode, pattern, description, priority, updated_by, source) "
                        "VALUES (%s, %s, %s, %s, %s, %s, 'template')",
                        (org_id, mode_key, rule["pattern"],
                         rule["description"], rule["priority"], user_id),
                    )
            cur.execute(pref_upsert, (org_key, org_id, "command_policy_allowlist", json.dumps("on")))
            cur.execute(pref_upsert, (org_key, org_id, "command_policy_denylist", json.dumps("on")))
            cur.execute(pref_upsert, (org_key, org_id, "command_policy_active_template", json.dumps(template_id)))
        conn.commit()

    invalidate_cache(org_id)
    return jsonify({"status": "applied", "template_id": template_id, **_list_states(org_id)})


@command_policies_bp.route("/command-policy-templates/active", methods=["DELETE", "OPTIONS"])
@require_permission("admin", "access")
def clear_active_template(user_id):
    if request.method == "OPTIONS":
        return jsonify({}), 200

    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No organization context"}), 403

    org_key = f"__org__{org_id}"
    pref_upsert = (
        "INSERT INTO user_preferences (user_id, org_id, preference_key, preference_value) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (user_id, org_id, preference_key) WHERE org_id IS NOT NULL DO UPDATE "
        "SET preference_value = EXCLUDED.preference_value, updated_at = CURRENT_TIMESTAMP"
    )
    from utils.db.connection_pool import db_pool
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM org_command_policies WHERE org_id = %s AND source = 'template'", (org_id,))
            cur.execute(pref_upsert, (org_key, org_id, "command_policy_active_template", json.dumps(None)))
        conn.commit()

    invalidate_cache(org_id)
    return jsonify({"status": "cleared", **_list_states(org_id)})
