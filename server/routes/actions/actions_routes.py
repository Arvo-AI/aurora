"""CRUD + trigger routes for Aurora Actions."""
import json
import logging
from datetime import datetime

from flask import jsonify, request
from utils.db.connection_pool import db_pool
from utils.auth.rbac_decorators import require_permission

from . import actions_bp

logger = logging.getLogger(__name__)

_VALID_TRIGGER_TYPES = ("manual", "on_incident")
_VALID_MODES = ("agent", "ask")


@actions_bp.route("", methods=["GET"])
@require_permission("actions", "read")
def list_actions(user_id):
    with db_pool.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT a.id, a.name, a.description, a.instructions, a.trigger_type,
                       a.trigger_config, a.mode, a.enabled, a.created_at, a.updated_at,
                       COUNT(r.id) AS run_count,
                       MAX(r.started_at) AS last_run_at,
                       (SELECT r2.status FROM action_runs r2
                        WHERE r2.action_id = a.id ORDER BY r2.started_at DESC LIMIT 1) AS last_run_status
                FROM actions a
                LEFT JOIN action_runs r ON r.action_id = a.id
                GROUP BY a.id
                ORDER BY a.created_at DESC
            """)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]

    for r in rows:
        r["id"] = str(r["id"])
        r["created_at"] = r["created_at"].isoformat() if r["created_at"] else None
        r["updated_at"] = r["updated_at"].isoformat() if r["updated_at"] else None
        r["last_run_at"] = r["last_run_at"].isoformat() if r["last_run_at"] else None
        r["run_count"] = r["run_count"] or 0

    return jsonify({"actions": rows})


@actions_bp.route("", methods=["POST"])
@require_permission("actions", "write")
def create_action(user_id):
    body = request.get_json(silent=True) or {}

    name = (body.get("name") or "").strip()
    instructions = (body.get("instructions") or "").strip()
    if not name or not instructions:
        return jsonify({"error": "name and instructions are required"}), 400
    if len(name) > 255:
        return jsonify({"error": "name must be 255 characters or fewer"}), 400

    trigger_type = body.get("trigger_type", "manual")
    mode = body.get("mode", "agent")
    if trigger_type not in _VALID_TRIGGER_TYPES:
        return jsonify({"error": f"trigger_type must be one of {_VALID_TRIGGER_TYPES}"}), 400
    if mode not in _VALID_MODES:
        return jsonify({"error": f"mode must be one of {_VALID_MODES}"}), 400

    description = (body.get("description") or "").strip() or None
    trigger_config = body.get("trigger_config", {})

    with db_pool.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT org_id FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            org_id = row[0] if row else None

            cur.execute(
                """INSERT INTO actions (org_id, created_by, name, description, instructions,
                   trigger_type, trigger_config, mode)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING id, created_at""",
                (org_id, user_id, name, description, instructions,
                 trigger_type, json.dumps(trigger_config), mode),
            )
            row = cur.fetchone()
            conn.commit()

    return jsonify({
        "id": str(row[0]),
        "name": name,
        "description": description,
        "instructions": instructions,
        "trigger_type": trigger_type,
        "mode": mode,
        "enabled": True,
        "created_at": row[1].isoformat() if row[1] else None,
    }), 201


@actions_bp.route("/<action_id>", methods=["GET"])
@require_permission("actions", "read")
def get_action(user_id, action_id):
    with db_pool.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, org_id, created_by, name, description, instructions,
                          trigger_type, trigger_config, mode, enabled, created_at, updated_at
                   FROM actions WHERE id = %s""",
                (action_id,),
            )
            cols = [d[0] for d in cur.description]
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "Action not found"}), 404
            action = dict(zip(cols, row))

            cur.execute(
                """SELECT id, status, incident_id, chat_session_id, trigger_context,
                          started_at, completed_at, error
                   FROM action_runs WHERE action_id = %s
                   ORDER BY started_at DESC LIMIT 20""",
                (action_id,),
            )
            run_cols = [d[0] for d in cur.description]
            runs = [dict(zip(run_cols, r)) for r in cur.fetchall()]

    action["id"] = str(action["id"])
    action["created_at"] = action["created_at"].isoformat() if action["created_at"] else None
    action["updated_at"] = action["updated_at"].isoformat() if action["updated_at"] else None

    for r in runs:
        r["id"] = str(r["id"])
        r["incident_id"] = str(r["incident_id"]) if r["incident_id"] else None
        r["chat_session_id"] = str(r["chat_session_id"]) if r["chat_session_id"] else None
        r["started_at"] = r["started_at"].isoformat() if r["started_at"] else None
        r["completed_at"] = r["completed_at"].isoformat() if r["completed_at"] else None
        if r["started_at"] and r["completed_at"]:
            sa = datetime.fromisoformat(r["started_at"])
            ca = datetime.fromisoformat(r["completed_at"])
            r["duration_ms"] = int((ca - sa).total_seconds() * 1000)

    return jsonify({"action": action, "recent_runs": runs})


@actions_bp.route("/<action_id>", methods=["PUT"])
@require_permission("actions", "write")
def update_action(user_id, action_id):
    body = request.get_json(silent=True) or {}

    sets, vals = [], []
    if "name" in body:
        name = (body["name"] or "").strip()
        if not name or len(name) > 255:
            return jsonify({"error": "name must be 1-255 characters"}), 400
        sets.append("name = %s")
        vals.append(name)
    if "description" in body:
        sets.append("description = %s")
        vals.append((body["description"] or "").strip() or None)
    if "instructions" in body:
        instructions = (body["instructions"] or "").strip()
        if not instructions:
            return jsonify({"error": "instructions cannot be empty"}), 400
        sets.append("instructions = %s")
        vals.append(instructions)
    if "trigger_type" in body:
        if body["trigger_type"] not in _VALID_TRIGGER_TYPES:
            return jsonify({"error": f"trigger_type must be one of {_VALID_TRIGGER_TYPES}"}), 400
        sets.append("trigger_type = %s")
        vals.append(body["trigger_type"])
    if "trigger_config" in body:
        sets.append("trigger_config = %s")
        vals.append(json.dumps(body["trigger_config"]))
    if "mode" in body:
        if body["mode"] not in _VALID_MODES:
            return jsonify({"error": f"mode must be one of {_VALID_MODES}"}), 400
        sets.append("mode = %s")
        vals.append(body["mode"])
    if "enabled" in body:
        sets.append("enabled = %s")
        vals.append(bool(body["enabled"]))

    if not sets:
        return jsonify({"error": "no fields to update"}), 400

    sets.append("updated_at = %s")
    vals.append(datetime.utcnow())
    vals.append(action_id)

    with db_pool.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE actions SET {', '.join(sets)} WHERE id = %s RETURNING id",
                vals,
            )
            if not cur.fetchone():
                return jsonify({"error": "Action not found"}), 404
            conn.commit()

    return get_action(user_id, action_id)


@actions_bp.route("/<action_id>", methods=["DELETE"])
@require_permission("actions", "write")
def delete_action(user_id, action_id):
    with db_pool.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM actions WHERE id = %s RETURNING id", (action_id,))
            if not cur.fetchone():
                return jsonify({"error": "Action not found"}), 404
            conn.commit()
    return "", 204


@actions_bp.route("/<action_id>/trigger", methods=["POST"])
@require_permission("actions", "write")
def trigger_action(user_id, action_id):
    body = request.get_json(silent=True) or {}
    trigger_context = {}
    if body.get("incident_id"):
        trigger_context["incident_id"] = body["incident_id"]
    if body.get("trigger_label"):
        trigger_context["trigger_label"] = body["trigger_label"]

    try:
        from services.actions.executor import dispatch_action
        run_id = dispatch_action(action_id, user_id, trigger_context)
    except ValueError as e:
        return jsonify({"error": str(e)}), 429 if "Rate limited" in str(e) else 400
    except Exception:
        logger.exception("[Actions] Failed to trigger action %s", action_id)
        return jsonify({"error": "Failed to trigger action"}), 500

    return jsonify({"run_id": run_id, "status": "pending"}), 202


@actions_bp.route("/<action_id>/runs", methods=["GET"])
@require_permission("actions", "read")
def list_runs(user_id, action_id):
    limit = min(int(request.args.get("limit", 50)), 200)
    offset = int(request.args.get("offset", 0))

    with db_pool.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, status, incident_id, chat_session_id, trigger_context,
                          started_at, completed_at, error
                   FROM action_runs WHERE action_id = %s
                   ORDER BY started_at DESC LIMIT %s OFFSET %s""",
                (action_id, limit, offset),
            )
            cols = [d[0] for d in cur.description]
            runs = [dict(zip(cols, r)) for r in cur.fetchall()]

    for r in runs:
        r["id"] = str(r["id"])
        r["incident_id"] = str(r["incident_id"]) if r["incident_id"] else None
        r["chat_session_id"] = str(r["chat_session_id"]) if r["chat_session_id"] else None
        r["started_at"] = r["started_at"].isoformat() if r["started_at"] else None
        r["completed_at"] = r["completed_at"].isoformat() if r["completed_at"] else None

    return jsonify({"runs": runs})
