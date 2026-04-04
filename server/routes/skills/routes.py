"""Flask API endpoints for skills CRUD and GitHub import."""

import json
import logging
import uuid
from typing import Optional, Tuple
from flask import Blueprint, jsonify, request
from utils.db.connection_pool import db_pool
from utils.web.cors_utils import create_cors_response
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request

logger = logging.getLogger(__name__)

skills_bp = Blueprint("skills", __name__)

VALID_SCOPES = ("global", "org", "user")
VALID_PROMPT_BEHAVIORS = ("supplement", "override", "exclusive")


def _set_rls(cursor, conn, user_id: str, org_id: Optional[str]) -> None:
    cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
    cursor.execute("SET myapp.current_org_id = %s;", (org_id or "",))
    conn.commit()


def _serialize_skill_row(row, include_body: bool = False) -> dict:
    """Serialize a skill DB row to a JSON-safe dict.

    List view columns (14): id, name, description, tags, providers, mode_restriction,
        prompt_behavior, scope, user_id, org_id, is_active, version, created_at, updated_at
    Detail view columns (16): same as above but with body and references_data inserted
        after description (positions 3 and 13).
    """
    if include_body:
        return {
            "id": str(row[0]),
            "name": row[1],
            "description": row[2],
            "body": row[3],
            "tags": row[4] or [],
            "providers": row[5] or [],
            "mode_restriction": row[6],
            "prompt_behavior": row[7],
            "scope": row[8],
            "user_id": row[9],
            "org_id": row[10],
            "is_active": row[11],
            "version": row[12],
            "references_data": row[13] or {},
            "created_at": row[14].isoformat() if row[14] else None,
            "updated_at": row[15].isoformat() if row[15] else None,
        }
    return {
        "id": str(row[0]),
        "name": row[1],
        "description": row[2],
        "tags": row[3] or [],
        "providers": row[4] or [],
        "mode_restriction": row[5],
        "prompt_behavior": row[6],
        "scope": row[7],
        "user_id": row[8],
        "org_id": row[9],
        "is_active": row[10],
        "version": row[11],
        "created_at": row[12].isoformat() if row[12] else None,
        "updated_at": row[13].isoformat() if row[13] else None,
    }


def _check_ownership(skill_scope: str, skill_org: Optional[str],
                      skill_owner: Optional[str], user_id: str,
                      org_id: Optional[str]) -> Optional[Tuple[dict, int]]:
    """Return (error_body, status_code) if user lacks permission, else None."""
    if skill_scope == "global":
        return {"error": "Cannot modify global (built-in) skills"}, 403
    if skill_scope == "user" and skill_owner != user_id:
        return {"error": "Cannot modify another user's private skill"}, 403
    if skill_scope == "org" and skill_org != org_id:
        return {"error": "Skill belongs to a different organization"}, 403
    return None


@skills_bp.route("/", methods=["GET", "OPTIONS"])
@require_permission("skills", "read")
def list_skills(user_id):
    if request.method == "OPTIONS":
        return create_cors_response()

    org_id = get_org_id_from_request()

    try:
        with db_pool.get_user_connection() as conn:
            with conn.cursor() as cursor:
                _set_rls(cursor, conn, user_id, org_id)
                cursor.execute(
                    """
                    SELECT id, name, description, tags, providers, mode_restriction,
                           prompt_behavior, scope, user_id, org_id, is_active,
                           version, created_at, updated_at
                    FROM skills
                    WHERE scope = 'global'
                       OR (scope = 'org' AND org_id = %s)
                       OR (scope = 'user' AND user_id = %s)
                    ORDER BY scope, name;
                    """,
                    (org_id, user_id),
                )
                rows = cursor.fetchall()

        return jsonify({"skills": [_serialize_skill_row(r) for r in rows]}), 200
    except Exception as e:
        logger.exception(f"[Skills] Error listing skills: {e}")
        return jsonify({"error": "Failed to list skills"}), 500


@skills_bp.route("/", methods=["POST"])
@require_permission("skills", "write")
def create_skill(user_id):
    if request.method == "OPTIONS":
        return create_cors_response()

    org_id = get_org_id_from_request()
    data = request.get_json(force=True, silent=True) or {}

    name = data.get("name", "").strip()
    description = data.get("description", "").strip()
    body = data.get("body", "").strip()

    if not name or not description or not body:
        return jsonify({"error": "name, description, and body are required"}), 400

    scope = data.get("scope", "org")
    if scope not in VALID_SCOPES or scope == "global":
        return jsonify({"error": "scope must be 'org' or 'user'"}), 400

    prompt_behavior = data.get("prompt_behavior", "supplement")
    if prompt_behavior not in VALID_PROMPT_BEHAVIORS:
        return jsonify({"error": f"prompt_behavior must be one of {VALID_PROMPT_BEHAVIORS}"}), 400

    tags = data.get("tags", [])
    providers = data.get("providers", [])
    mode_restriction = data.get("mode_restriction")
    version = data.get("version", "1.0")
    references_data = data.get("references_data", {})
    is_active = data.get("is_active", True)

    skill_id = str(uuid.uuid4())
    skill_user_id = user_id if scope == "user" else None
    skill_org_id = org_id if scope in ("org", "user") else None

    try:
        with db_pool.get_user_connection() as conn:
            with conn.cursor() as cursor:
                _set_rls(cursor, conn, user_id, org_id)
                cursor.execute(
                    """
                    INSERT INTO skills (id, name, description, body, tags, providers,
                                        mode_restriction, prompt_behavior, scope,
                                        user_id, org_id, is_active, version, references_data)
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    RETURNING id, created_at;
                    """,
                    (
                        skill_id, name, description, body,
                        json.dumps(tags), json.dumps(providers),
                        mode_restriction, prompt_behavior, scope,
                        skill_user_id, skill_org_id, is_active, version,
                        json.dumps(references_data),
                    ),
                )
                result = cursor.fetchone()
                conn.commit()

        _invalidate_skill_cache()

        return jsonify({
            "id": str(result[0]),
            "name": name,
            "created_at": result[1].isoformat() if result[1] else None,
        }), 201
    except Exception as e:
        if "idx_skills_name_scope" in str(e):
            return jsonify({"error": f"A skill named '{name}' already exists in this scope"}), 409
        logger.exception(f"[Skills] Error creating skill: {e}")
        return jsonify({"error": "Failed to create skill"}), 500


@skills_bp.route("/<skill_id>", methods=["GET", "OPTIONS"])
@require_permission("skills", "read")
def get_skill(user_id, skill_id):
    if request.method == "OPTIONS":
        return create_cors_response()

    org_id = get_org_id_from_request()

    try:
        with db_pool.get_user_connection() as conn:
            with conn.cursor() as cursor:
                _set_rls(cursor, conn, user_id, org_id)
                cursor.execute(
                    """
                    SELECT id, name, description, body, tags, providers,
                           mode_restriction, prompt_behavior, scope, user_id, org_id,
                           is_active, version, references_data, created_at, updated_at
                    FROM skills
                    WHERE id = %s
                      AND (
                        scope = 'global'
                        OR (scope = 'org' AND org_id = %s)
                        OR (scope = 'user' AND user_id = %s)
                      );
                    """,
                    (skill_id, org_id, user_id),
                )
                row = cursor.fetchone()

        if not row:
            return jsonify({"error": "Skill not found"}), 404

        return jsonify(_serialize_skill_row(row, include_body=True)), 200
    except Exception as e:
        logger.exception(f"[Skills] Error getting skill {skill_id}: {e}")
        return jsonify({"error": "Failed to get skill"}), 500


@skills_bp.route("/<skill_id>", methods=["PUT"])
@require_permission("skills", "write")
def update_skill(user_id, skill_id):
    if request.method == "OPTIONS":
        return create_cors_response()

    org_id = get_org_id_from_request()
    data = request.get_json(force=True, silent=True) or {}

    # Validate prompt_behavior before building query
    if "prompt_behavior" in data and data["prompt_behavior"] not in VALID_PROMPT_BEHAVIORS:
        return jsonify({"error": f"prompt_behavior must be one of {VALID_PROMPT_BEHAVIORS}"}), 400

    try:
        with db_pool.get_user_connection() as conn:
            with conn.cursor() as cursor:
                _set_rls(cursor, conn, user_id, org_id)

                cursor.execute(
                    "SELECT scope, org_id, user_id FROM skills WHERE id = %s;",
                    (skill_id,),
                )
                row = cursor.fetchone()
                if not row:
                    return jsonify({"error": "Skill not found"}), 404

                denied = _check_ownership(row[0], row[1], row[2], user_id, org_id)
                if denied:
                    return jsonify(denied[0]), denied[1]

                updates = []
                params = []
                for field_name, col, cast in [
                    ("name", "name", None),
                    ("description", "description", None),
                    ("body", "body", None),
                    ("tags", "tags", "::jsonb"),
                    ("providers", "providers", "::jsonb"),
                    ("mode_restriction", "mode_restriction", None),
                    ("prompt_behavior", "prompt_behavior", None),
                    ("is_active", "is_active", None),
                    ("version", "version", None),
                    ("references_data", "references_data", "::jsonb"),
                ]:
                    if field_name in data:
                        val = data[field_name]
                        if cast:
                            updates.append(f"{col} = %s{cast}")
                            params.append(json.dumps(val))
                        else:
                            updates.append(f"{col} = %s")
                            params.append(val)

                if not updates:
                    return jsonify({"error": "No fields to update"}), 400

                updates.append("updated_at = CURRENT_TIMESTAMP")
                params.append(skill_id)

                sql = f"UPDATE skills SET {', '.join(updates)} WHERE id = %s RETURNING id, updated_at;"
                cursor.execute(sql, params)
                result = cursor.fetchone()
                conn.commit()

        _invalidate_skill_cache()

        return jsonify({
            "id": str(result[0]),
            "updated_at": result[1].isoformat() if result[1] else None,
        }), 200
    except Exception as e:
        if "idx_skills_name_scope" in str(e):
            return jsonify({"error": "A skill with that name already exists in this scope"}), 409
        logger.exception(f"[Skills] Error updating skill {skill_id}: {e}")
        return jsonify({"error": "Failed to update skill"}), 500


@skills_bp.route("/<skill_id>", methods=["DELETE"])
@require_permission("skills", "write")
def delete_skill(user_id, skill_id):
    if request.method == "OPTIONS":
        return create_cors_response()

    org_id = get_org_id_from_request()

    try:
        with db_pool.get_user_connection() as conn:
            with conn.cursor() as cursor:
                _set_rls(cursor, conn, user_id, org_id)

                cursor.execute(
                    "SELECT scope, org_id, user_id FROM skills WHERE id = %s;",
                    (skill_id,),
                )
                row = cursor.fetchone()
                if not row:
                    return jsonify({"error": "Skill not found"}), 404

                denied = _check_ownership(row[0], row[1], row[2], user_id, org_id)
                if denied:
                    return jsonify(denied[0]), denied[1]

                cursor.execute("DELETE FROM skills WHERE id = %s;", (skill_id,))
                conn.commit()

        _invalidate_skill_cache()

        return jsonify({"deleted": True}), 200
    except Exception as e:
        logger.exception(f"[Skills] Error deleting skill {skill_id}: {e}")
        return jsonify({"error": "Failed to delete skill"}), 500


# --- GitHub Import Endpoints ---


@skills_bp.route("/import/discover", methods=["POST", "OPTIONS"])
@require_permission("skills", "write")
def import_discover(user_id):
    if request.method == "OPTIONS":
        return create_cors_response()

    data = request.get_json(force=True, silent=True) or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "url is required"}), 400

    try:
        from .github_import import resolve_repo, discover_skills

        owner, repo = resolve_repo(url)
        previews = discover_skills(owner, repo)

        return jsonify({
            "owner": owner,
            "repo": repo,
            "skills": [
                {"name": p.name, "description": p.description, "path": p.path}
                for p in previews
            ],
        }), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.exception(f"[Skills] Error discovering skills from {url}: {e}")
        return jsonify({"error": f"Failed to discover skills: {str(e)}"}), 500


@skills_bp.route("/import/install", methods=["POST", "OPTIONS"])
@require_permission("skills", "write")
def import_install(user_id):
    if request.method == "OPTIONS":
        return create_cors_response()

    org_id = get_org_id_from_request()
    data = request.get_json(force=True, silent=True) or {}
    url = data.get("url", "").strip()
    skill_paths = data.get("skill_paths", [])

    if not url or not skill_paths:
        return jsonify({"error": "url and skill_paths are required"}), 400

    try:
        from .github_import import resolve_repo, fetch_skill

        owner, repo = resolve_repo(url)
        installed = []

        # Use a single connection for the entire batch
        with db_pool.get_user_connection() as conn:
            with conn.cursor() as cursor:
                _set_rls(cursor, conn, user_id, org_id)

                for path in skill_paths:
                    try:
                        skill_data = fetch_skill(owner, repo, path)
                        skill_id = str(uuid.uuid4())

                        cursor.execute(
                            """
                            INSERT INTO skills (id, name, description, body, tags, providers,
                                                scope, org_id, references_data)
                            VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, 'org', %s, %s::jsonb)
                            ON CONFLICT (name, scope, COALESCE(org_id, ''), COALESCE(user_id, ''))
                            DO UPDATE SET
                                description = EXCLUDED.description,
                                body = EXCLUDED.body,
                                tags = EXCLUDED.tags,
                                providers = EXCLUDED.providers,
                                references_data = EXCLUDED.references_data,
                                updated_at = CURRENT_TIMESTAMP
                            RETURNING id;
                            """,
                            (
                                skill_id,
                                skill_data.name,
                                skill_data.description,
                                skill_data.body,
                                json.dumps(skill_data.tags),
                                json.dumps(skill_data.providers),
                                org_id,
                                json.dumps(skill_data.references_data),
                            ),
                        )
                        installed.append({"name": skill_data.name, "path": path})
                    except Exception as e:
                        logger.warning(f"[Skills] Failed to import skill from {path}: {e}")
                        installed.append({"name": path, "path": path, "error": str(e)})

                conn.commit()

        _invalidate_skill_cache()

        return jsonify({"installed": installed}), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.exception(f"[Skills] Error installing skills from {url}: {e}")
        return jsonify({"error": f"Failed to install skills: {str(e)}"}), 500


def _invalidate_skill_cache():
    try:
        from chat.backend.agent.skills.skill_store import SkillStore
        SkillStore.get_instance().invalidate_cache()
    except Exception:
        pass
