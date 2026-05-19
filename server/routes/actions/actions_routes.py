"""CRUD + trigger routes for Aurora Actions."""
import json
import logging
import re
from datetime import datetime, timezone

from flask import jsonify, request
from utils.db.connection_pool import db_pool
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import set_rls_context
from services.actions.system_actions import seed_system_actions, SYSTEM_ACTIONS

_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)

_ERR_INTERNAL = "Internal server error"


def _validate_uuid(value: str) -> bool:
    return bool(_UUID_RE.match(value))


_ERR_NOT_FOUND = "Action not found"

from . import actions_bp

logger = logging.getLogger(__name__)

_VALID_TRIGGER_TYPES = ("manual", "on_incident", "on_schedule")
_VALID_MODES = ("agent", "ask")


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


def _validate_trigger_config(body):
    """Validate trigger_config field. Returns (sanitized_config, error_msg)."""
    tc = body["trigger_config"]
    if tc is not None and not isinstance(tc, dict):
        return None, "trigger_config must be a JSON object or null"
    effective_type = body.get("trigger_type") or None
    if effective_type == "on_schedule" or (not effective_type and "interval_seconds" in (tc or {})):
        interval = (tc or {}).get("interval_seconds")
        if not isinstance(interval, (int, float)) or int(interval) < 300:
            return None, "on_schedule requires trigger_config.interval_seconds >= 300"
        tc = {"interval_seconds": int(interval)}
    return tc, None


def _validate_name(body):
    name = (body["name"] or "").strip()
    if not name or len(name) > 255:
        return None, "name must be 1-255 characters"
    return name, None


def _validate_instructions(body):
    instructions = (body["instructions"] or "").strip()
    if not instructions:
        return None, "instructions cannot be empty"
    return instructions, None


def _validate_description(body):
    return (body["description"] or "").strip() or None, None


def _validate_trigger_type(body):
    val = body["trigger_type"]
    if val not in _VALID_TRIGGER_TYPES:
        return None, f"trigger_type must be one of {_VALID_TRIGGER_TYPES}"
    return val, None


def _validate_mode(body):
    val = body["mode"]
    if val not in _VALID_MODES:
        return None, f"mode must be one of {_VALID_MODES}"
    return val, None


def _validate_enabled(body):
    val = body["enabled"]
    if not isinstance(val, bool):
        return None, "enabled must be a boolean"
    return val, None


def _validate_trigger_config_json(body):
    tc, err = _validate_trigger_config(body)
    if err:
        return None, err
    return json.dumps(tc), None


_FIELD_VALIDATORS = [
    ("name", _validate_name),
    ("description", _validate_description),
    ("instructions", _validate_instructions),
    ("trigger_type", _validate_trigger_type),
    ("trigger_config", _validate_trigger_config_json),
    ("mode", _validate_mode),
    ("enabled", _validate_enabled),
]


def _parse_update_fields(body):
    """Validate and extract update columns/vals from request body.

    Returns (columns, vals, error_msg) where error_msg is a string on failure.
    columns contains only names from _COL_FRAGMENTS.
    """
    columns, vals = [], []
    for field, validator in _FIELD_VALIDATORS:
        if field not in body:
            continue
        val, err = validator(body)
        if err:
            return None, None, err
        columns.append(field)
        vals.append(val)
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
    try:
        with db_pool.get_connection() as conn:
            with conn.cursor() as cur:
                # Ensure system actions exist for this org
                cur.execute("SELECT org_id FROM users WHERE id = %s", (user_id,))
                org_row = cur.fetchone()
                if org_row and org_row[0]:
                    expected_keys = [a["system_key"] for a in SYSTEM_ACTIONS]
                    cur.execute(
                        "SELECT system_key FROM actions WHERE org_id = %s AND is_system = true AND system_key = ANY(%s)",
                        (org_row[0], expected_keys),
                    )
                    existing_keys = {row[0] for row in cur.fetchall()}
                    if len(existing_keys) < len(expected_keys):
                        try:
                            seed_system_actions(org_row[0], user_id)
                        except Exception:
                            logger.debug("Failed to lazy-seed system actions")

                cur.execute("""
                    SELECT a.id, a.name, a.description, a.instructions, a.trigger_type,
                           a.trigger_config, a.mode, a.enabled, a.created_at, a.updated_at,
                           a.is_system, a.system_key, a.default_instructions,
                           COUNT(r.id) AS run_count,
                           MAX(r.started_at) AS last_run_at,
                           (SELECT r2.status FROM action_runs r2
                            WHERE r2.action_id = a.id ORDER BY r2.started_at DESC LIMIT 1) AS last_run_status
                    FROM actions a
                    LEFT JOIN action_runs r ON r.action_id = a.id
                    GROUP BY a.id
                    ORDER BY a.is_system DESC, a.created_at DESC
                """)
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, row)) for row in cur.fetchall()]

        for r in rows:
            r["id"] = str(r["id"])
            r["created_at"] = (r["created_at"].isoformat() + "Z") if r["created_at"] else None
            r["updated_at"] = (r["updated_at"].isoformat() + "Z") if r["updated_at"] else None
            r["last_run_at"] = (r["last_run_at"].isoformat() + "Z") if r["last_run_at"] else None
            r["run_count"] = r["run_count"] or 0
            r["is_system"] = r.get("is_system", False)
            r["is_modified"] = (
                r["is_system"]
                and r.get("default_instructions")
                and r.get("instructions") != r.get("default_instructions")
            )
            r.pop("default_instructions", None)

        return jsonify({"actions": rows})
    except Exception:
        logger.exception("Failed to list actions")
        return jsonify({"error": _ERR_INTERNAL}), 500


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

            if not org_id:
                return jsonify({"error": "User has no organization"}), 400

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
                          trigger_type, trigger_config, mode, enabled,
                          is_system, system_key, default_instructions,
                          created_at, updated_at
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
    action["is_system"] = action.get("is_system", False)
    action["is_modified"] = (
        action["is_system"]
        and action.get("default_instructions")
        and action.get("instructions") != action.get("default_instructions")
    )
    action.pop("default_instructions", None)

    for r in runs:
        r["id"] = str(r["id"])
        r["incident_id"] = str(r["incident_id"]) if r["incident_id"] else None
        r["chat_session_id"] = str(r["chat_session_id"]) if r["chat_session_id"] else None
        if r["started_at"] and r["completed_at"]:
            r["duration_ms"] = max(0, int((r["completed_at"] - r["started_at"]).total_seconds() * 1000))
        r["started_at"] = (r["started_at"].isoformat() + "Z") if r["started_at"] else None
        r["completed_at"] = (r["completed_at"].isoformat() + "Z") if r["completed_at"] else None

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

    set_parts = [_COL_FRAGMENTS[col] for col in columns]
    sql = "UPDATE actions SET " + ", ".join(set_parts) + " WHERE id = %s RETURNING id"

    try:
        with db_pool.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, vals)
                if not cur.fetchone():
                    return jsonify({"error": _ERR_NOT_FOUND}), 404
                conn.commit()
    except Exception:
        logger.exception("Failed to update action")
        return jsonify({"error": _ERR_INTERNAL}), 500

    return _get_action_response(action_id)


@actions_bp.route("/<action_id>", methods=["DELETE"])
@require_permission("actions", "write")
def delete_action(user_id, action_id):
    if not _validate_uuid(action_id):
        return jsonify({"error": _ERR_NOT_FOUND}), 404
    try:
        with db_pool.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT is_system FROM actions WHERE id = %s", (action_id,))
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": _ERR_NOT_FOUND}), 404
                if row[0]:
                    return jsonify({"error": "System actions cannot be deleted. You can disable them instead."}), 403
                cur.execute("DELETE FROM actions WHERE id = %s", (action_id,))
                conn.commit()
    except Exception:
        logger.exception("Failed to delete action")
        return jsonify({"error": "Failed to delete action"}), 500
    return "", 204


@actions_bp.route("/<action_id>/restore-default", methods=["POST"])
@require_permission("actions", "write")
def restore_default(user_id, action_id):
    """Restore a system action's instructions to the built-in default."""
    if not _validate_uuid(action_id):
        return jsonify({"error": _ERR_NOT_FOUND}), 404
    try:
        with db_pool.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT is_system, default_instructions FROM actions WHERE id = %s",
                    (action_id,),
                )
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": _ERR_NOT_FOUND}), 404
                if not row[0]:
                    return jsonify({"error": "Only system actions can be restored to default"}), 400
                cur.execute(
                    "UPDATE actions SET instructions = %s, updated_at = %s WHERE id = %s",
                    (row[1], datetime.now(timezone.utc), action_id),
                )
                conn.commit()
    except Exception:
        logger.exception("Failed to restore default instructions")
        return jsonify({"error": _ERR_INTERNAL}), 500
    return _get_action_response(action_id)


@actions_bp.route("/<action_id>/trigger", methods=["POST"])
@require_permission("actions", "write")
def trigger_action(user_id, action_id):
    if not _validate_uuid(action_id):
        return jsonify({"error": _ERR_NOT_FOUND}), 404
    body = request.get_json(silent=True) or {}
    trigger_context = {}
    if body.get("incident_id"):
        if not _validate_uuid(body["incident_id"]):
            return jsonify({"error": "Invalid incident_id format"}), 400
        trigger_context["incident_id"] = body["incident_id"]
    if body.get("trigger_label"):
        trigger_context["trigger_label"] = body["trigger_label"]

    # on_incident actions (like postmortem) require an incident_id
    try:
        with db_pool.get_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[Actions:trigger]")
                cur.execute(
                    "SELECT trigger_type FROM actions WHERE id = %s",
                    (action_id,),
                )
                row = cur.fetchone()
                if row and row[0] == "on_incident" and not trigger_context.get("incident_id"):
                    return jsonify({"error": "This action requires an incident. Trigger it from an incident page instead."}), 400
    except Exception:
        logger.exception("Failed to verify on_incident precondition")
        return jsonify({"error": "Unable to validate action context. Please retry."}), 503

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
        limit = max(min(int(request.args.get("limit", 50)), 200), 1)
        offset = max(int(request.args.get("offset", 0)), 0)
    except (ValueError, TypeError):
        return jsonify({"error": "limit and offset must be integers"}), 400

    try:
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
            if r["started_at"] and r["completed_at"]:
                r["duration_ms"] = max(0, int((r["completed_at"] - r["started_at"]).total_seconds() * 1000))
            r["started_at"] = (r["started_at"].isoformat() + "Z") if r["started_at"] else None
            r["completed_at"] = (r["completed_at"].isoformat() + "Z") if r["completed_at"] else None

        return jsonify({"runs": runs})
    except Exception:
        logger.exception("Failed to list runs")
        return jsonify({"error": _ERR_INTERNAL}), 500
