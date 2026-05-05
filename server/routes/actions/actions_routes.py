"""CRUD + trigger routes for Aurora Actions."""
import json
import logging
import re
from datetime import datetime, timezone

from flask import jsonify, request
from utils.db.connection_pool import db_pool
from utils.auth.rbac_decorators import require_permission

_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)


def _validate_uuid(value: str) -> bool:
    return bool(_UUID_RE.match(value))


_ERR_NOT_FOUND = "Action not found"

from . import actions_bp

logger = logging.getLogger(__name__)

_VALID_TRIGGER_TYPES = ("manual", "on_incident", "on_schedule")
_VALID_MODES = ("agent", "ask")


_UPDATABLE_COLUMNS = frozenset({"name", "description", "instructions", "trigger_type", "trigger_config", "mode", "enabled"})


def _parse_update_fields(body):
    """Validate and extract update columns/vals from request body.

    Returns (columns, vals, error_msg) where error_msg is a string on failure.
    columns contains only names from _UPDATABLE_COLUMNS.
    """
    columns, vals = [], []
    if "name" in body:
        name = (body["name"] or "").strip()
        if not name or len(name) > 255:
            return None, None, "name must be 1-255 characters"
        columns.append("name")
        vals.append(name)
    if "description" in body:
        columns.append("description")
        vals.append((body["description"] or "").strip() or None)
    if "instructions" in body:
        instructions = (body["instructions"] or "").strip()
        if not instructions:
            return None, None, "instructions cannot be empty"
        columns.append("instructions")
        vals.append(instructions)
    if "trigger_type" in body:
        if body["trigger_type"] not in _VALID_TRIGGER_TYPES:
            return None, None, f"trigger_type must be one of {_VALID_TRIGGER_TYPES}"
        columns.append("trigger_type")
        vals.append(body["trigger_type"])
    if "trigger_config" in body:
        tc = body["trigger_config"]
        effective_type = body.get("trigger_type") or None
        if effective_type == "on_schedule" or (not effective_type and "interval_seconds" in (tc or {})):
            interval = (tc or {}).get("interval_seconds")
            if not isinstance(interval, (int, float)) or int(interval) < 300:
                return None, None, "on_schedule requires trigger_config.interval_seconds >= 300"
            tc = {"interval_seconds": int(interval)}
        columns.append("trigger_config")
        vals.append(json.dumps(tc))
    if "mode" in body:
        if body["mode"] not in _VALID_MODES:
            return None, None, f"mode must be one of {_VALID_MODES}"
        columns.append("mode")
        vals.append(body["mode"])
    if "enabled" in body:
        columns.append("enabled")
        vals.append(bool(body["enabled"]))
    return columns, vals, None


def _validate_create_fields(name, instructions, body):
    """Return error message string if validation fails, else None."""
    if not name or not instructions:
        return "name and instructions are required"
    if len(name) > 255:
        return "name must be 255 characters or fewer"
    trigger_type = body.get("trigger_type", "manual")
    mode = body.get("mode", "agent")
    if trigger_type not in _VALID_TRIGGER_TYPES:
        return f"trigger_type must be one of {_VALID_TRIGGER_TYPES}"
    if mode not in _VALID_MODES:
        return f"mode must be one of {_VALID_MODES}"
    return None


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
        r["created_at"] = (r["created_at"].isoformat() + "Z") if r["created_at"] else None
        r["updated_at"] = (r["updated_at"].isoformat() + "Z") if r["updated_at"] else None
        r["last_run_at"] = (r["last_run_at"].isoformat() + "Z") if r["last_run_at"] else None
        r["run_count"] = r["run_count"] or 0

    return jsonify({"actions": rows})


@actions_bp.route("", methods=["POST"])
@require_permission("actions", "write")
def create_action(user_id):
    body = request.get_json(silent=True) or {}

    name = (body.get("name") or "").strip()
    instructions = (body.get("instructions") or "").strip()
    err = _validate_create_fields(name, instructions, body)
    if err:
        return jsonify({"error": err}), 400

    description = (body.get("description") or "").strip() or None
    trigger_type = body.get("trigger_type", "manual")
    mode = body.get("mode", "agent")
    trigger_config = body.get("trigger_config", {})

    if trigger_type == "on_schedule":
        interval = trigger_config.get("interval_seconds")
        if not isinstance(interval, (int, float)) or int(interval) < 300:
            return jsonify({"error": "on_schedule requires trigger_config.interval_seconds >= 300"}), 400
        trigger_config = {"interval_seconds": int(interval)}

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
        "created_at": (row[1].isoformat() + "Z") if row[1] else None,
    }), 201


@actions_bp.route("/<action_id>", methods=["GET"])
@require_permission("actions", "read")
def get_action(user_id, action_id):
    if not _validate_uuid(action_id):
        return jsonify({"error": _ERR_NOT_FOUND}), 404
    return _get_action_response(action_id)


def _get_action_response(action_id):
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
                return jsonify({"error": _ERR_NOT_FOUND}), 404
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
    action["created_at"] = (action["created_at"].isoformat() + "Z") if action["created_at"] else None
    action["updated_at"] = (action["updated_at"].isoformat() + "Z") if action["updated_at"] else None

    for r in runs:
        r["id"] = str(r["id"])
        r["incident_id"] = str(r["incident_id"]) if r["incident_id"] else None
        r["chat_session_id"] = str(r["chat_session_id"]) if r["chat_session_id"] else None
        r["started_at"] = (r["started_at"].isoformat() + "Z") if r["started_at"] else None
        r["completed_at"] = (r["completed_at"].isoformat() + "Z") if r["completed_at"] else None
        if r["started_at"] and r["completed_at"]:
            sa = datetime.fromisoformat(r["started_at"])
            ca = datetime.fromisoformat(r["completed_at"])
            r["duration_ms"] = int((ca - sa).total_seconds() * 1000)

    return jsonify({"action": action, "recent_runs": runs})


@actions_bp.route("/<action_id>", methods=["PUT"])
@require_permission("actions", "write")
def update_action(user_id, action_id):
    if not _validate_uuid(action_id):
        return jsonify({"error": _ERR_NOT_FOUND}), 404
    body = request.get_json(silent=True) or {}

    columns, vals, err = _parse_update_fields(body)
    if err:
        return jsonify({"error": err}), 400

    if not columns:
        return jsonify({"error": "no fields to update"}), 400

    columns.append("updated_at")
    vals.append(datetime.now(timezone.utc))
    vals.append(action_id)

    # Build SET clause exclusively from constant strings; columns only determines which are included
    _COL_FRAGMENTS = {
        "name": "name = %s",
        "description": "description = %s",
        "instructions": "instructions = %s",
        "trigger_type": "trigger_type = %s",
        "trigger_config": "trigger_config = %s",
        "mode": "mode = %s",
        "enabled": "enabled = %s",
        "updated_at": "updated_at = %s",
    }
    set_parts = [_COL_FRAGMENTS[col] for col in columns if col in _COL_FRAGMENTS]
    sql = "UPDATE actions SET " + ", ".join(set_parts) + " WHERE id = %s RETURNING id"

    with db_pool.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, vals)
            if not cur.fetchone():
                return jsonify({"error": _ERR_NOT_FOUND}), 404
            conn.commit()

    return _get_action_response(action_id)


@actions_bp.route("/<action_id>", methods=["DELETE"])
@require_permission("actions", "write")
def delete_action(user_id, action_id):
    if not _validate_uuid(action_id):
        return jsonify({"error": _ERR_NOT_FOUND}), 404
    try:
        with db_pool.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM actions WHERE id = %s RETURNING id", (action_id,))
                if not cur.fetchone():
                    return jsonify({"error": _ERR_NOT_FOUND}), 404
                conn.commit()
    except Exception:
        logger.exception("Failed to delete action")
        return jsonify({"error": "Failed to delete action"}), 500
    return "", 204


@actions_bp.route("/<action_id>/trigger", methods=["POST"])
@require_permission("actions", "write")
def trigger_action(user_id, action_id):
    if not _validate_uuid(action_id):
        return jsonify({"error": _ERR_NOT_FOUND}), 404
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
        if "Rate limited" in str(e):
            return jsonify({"error": "Rate limited — try again later"}), 429
        return jsonify({"error": "Invalid action configuration"}), 400
    except Exception:
        logger.exception("Failed to trigger action")
        return jsonify({"error": "Failed to trigger action"}), 500

    return jsonify({"run_id": run_id, "status": "pending"}), 202


@actions_bp.route("/<action_id>/runs", methods=["GET"])
@require_permission("actions", "read")
def list_runs(user_id, action_id):
    if not _validate_uuid(action_id):
        return jsonify({"error": _ERR_NOT_FOUND}), 404
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
        offset = max(int(request.args.get("offset", 0)), 0)
    except (ValueError, TypeError):
        return jsonify({"error": "limit and offset must be integers"}), 400

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
        r["started_at"] = (r["started_at"].isoformat() + "Z") if r["started_at"] else None
        r["completed_at"] = (r["completed_at"].isoformat() + "Z") if r["completed_at"] else None

    return jsonify({"runs": runs})
