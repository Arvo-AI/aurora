"""API routes for artifact CRUD, version history, and restore.

Artifacts are persistent markdown documents Aurora maintains over time (living
findings lists, cost reports, runbooks). The agent writes them by title via
artifact_tool; this blueprint backs the Monitor → Artifacts UI and the MCP
docs tools. Both write paths share services.artifacts.store so versioning
never drifts between them.
"""

import logging
from functools import wraps

import psycopg2.errors
from flask import Blueprint, jsonify, request

from routes.audit_routes import record_audit_event
from services.artifacts.store import create_version, upsert_artifact_by_title
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request, set_rls_context
from utils.db.connection_pool import db_pool
from utils.query_helpers import iso_utc
from utils.validation import is_valid_uuid

logger = logging.getLogger(__name__)

artifact_bp = Blueprint("artifact", __name__)
_LOG_PREFIX = "[Artifact]"
_MAX_CONTENT = 100000
_MAX_TITLE = 500
_ARTIFACT_NOT_FOUND = "Artifact not found"


def _serialize_artifact(row, *, include_content: bool) -> dict:
    """Build a camelCase artifact dict from a row of
    (id, title, content, last_edited_by, created_at, updated_at, version_number).
    """
    data = {
        "id": str(row[0]),
        "title": row[1],
        "lastEditedBy": row[3],
        "createdAt": iso_utc(row[4]),
        "updatedAt": iso_utc(row[5]),
        "version": row[6] or 0,
    }
    if include_content:
        data["content"] = row[2] or ""
    return data


def with_artifact(fn):
    """Validate artifact_id, resolve org_id, open DB, set RLS, confirm the
    artifact exists. Injects org_id, conn, cursor as keyword args. 404 if absent.
    """
    @wraps(fn)
    def wrapper(user_id, artifact_id, *args, **kwargs):
        if not is_valid_uuid(artifact_id):
            return jsonify({"error": "Invalid artifact ID"}), 400

        org_id = get_org_id_from_request()

        try:
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cursor:
                    if set_rls_context(cursor, conn, user_id, log_prefix=_LOG_PREFIX) is None:
                        # Org couldn't be resolved → RLS unset → every query would
                        # default-deny. Surface that rather than a misleading 404.
                        return jsonify({"error": "Unable to resolve organization context"}), 500

                    cursor.execute(
                        "SELECT id FROM artifacts WHERE id = %s AND org_id = %s",
                        (artifact_id, org_id),
                    )
                    if not cursor.fetchone():
                        return jsonify({"error": _ARTIFACT_NOT_FOUND}), 404

                    return fn(
                        user_id, artifact_id, *args,
                        org_id=org_id, conn=conn, cursor=cursor, **kwargs,
                    )
        except Exception:
            logger.exception("%s %s failed for artifact %s", _LOG_PREFIX, fn.__name__, artifact_id)
            return jsonify({"error": f"Failed to {fn.__name__.replace('_', ' ')}"}), 500

    return wrapper


@artifact_bp.route("/api/artifacts", methods=["GET"])
@require_permission("artifacts", "read")
def list_or_get_artifacts(user_id):
    """List artifacts (no ?title) or fetch one by exact title (?title=)."""
    org_id = get_org_id_from_request()
    title = request.args.get("title")

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix=_LOG_PREFIX)

                if title:
                    cursor.execute(
                        """SELECT a.id, a.title, a.content, a.last_edited_by,
                                  a.created_at, a.updated_at, COALESCE(v.version_number, 0)
                           FROM artifacts a
                           LEFT JOIN artifact_versions v ON a.current_version_id = v.id
                           WHERE a.org_id = %s AND a.title = %s""",
                        (org_id, title.strip()),
                    )
                    row = cursor.fetchone()
                    if not row:
                        return jsonify({"error": _ARTIFACT_NOT_FOUND}), 404
                    return jsonify({"artifact": _serialize_artifact(row, include_content=True)})

                cursor.execute(
                    """SELECT a.id, a.title, NULL, a.last_edited_by,
                              a.created_at, a.updated_at, COALESCE(v.version_number, 0)
                       FROM artifacts a
                       LEFT JOIN artifact_versions v ON a.current_version_id = v.id
                       WHERE a.org_id = %s
                       ORDER BY a.updated_at DESC""",
                    (org_id,),
                )
                rows = cursor.fetchall()

        artifacts = [_serialize_artifact(r, include_content=False) for r in rows]
        return jsonify({"artifacts": artifacts})

    except Exception:
        logger.exception("%s Failed to list artifacts for user %s", _LOG_PREFIX, user_id)
        return jsonify({"error": "Failed to fetch artifacts"}), 500


@artifact_bp.route("/api/artifacts", methods=["POST"])
@require_permission("artifacts", "write")
def create_artifact(user_id):
    """Create (or replace by title) an artifact from the UI / MCP. Marked as a
    user edit so the agent treats it as human-authored."""
    data = request.get_json(force=True, silent=True) or {}

    title = (data.get("title") or "").strip()
    content = data.get("content")
    if not title:
        return jsonify({"error": "Title is required"}), 400
    if len(title) > _MAX_TITLE:
        return jsonify({"error": f"Title exceeds maximum length of {_MAX_TITLE} characters"}), 400
    if not isinstance(content, str) or not content.strip():
        return jsonify({"error": "Content is required"}), 400
    if len(content) > _MAX_CONTENT:
        return jsonify({"error": f"Content exceeds maximum length of {_MAX_CONTENT} characters"}), 400

    org_id = get_org_id_from_request()

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix=_LOG_PREFIX)
                artifact_id, version = upsert_artifact_by_title(
                    cursor, org_id, user_id, title, content, source="manual",
                )
                conn.commit()
    except Exception:
        logger.exception("%s Failed to create artifact for user %s", _LOG_PREFIX, user_id)
        return jsonify({"error": "Failed to create artifact"}), 500

    record_audit_event(org_id, user_id, "create_artifact", "artifact", artifact_id,
                       {"title": title}, request)
    return jsonify({"id": artifact_id, "version": version}), 201


@artifact_bp.route("/api/artifacts/<artifact_id>", methods=["GET"])
@require_permission("artifacts", "read")
@with_artifact
def get_artifact(user_id, artifact_id, *, org_id, conn, cursor, **kwargs):
    cursor.execute(
        """SELECT a.id, a.title, a.content, a.last_edited_by,
                  a.created_at, a.updated_at, COALESCE(v.version_number, 0)
           FROM artifacts a
           LEFT JOIN artifact_versions v ON a.current_version_id = v.id
           WHERE a.id = %s AND a.org_id = %s""",
        (artifact_id, org_id),
    )
    row = cursor.fetchone()
    if not row:
        return jsonify({"error": _ARTIFACT_NOT_FOUND}), 404
    return jsonify({"artifact": _serialize_artifact(row, include_content=True)})


@artifact_bp.route("/api/artifacts/<artifact_id>", methods=["PATCH"])
@require_permission("artifacts", "write")
@with_artifact
def update_artifact(user_id, artifact_id, *, org_id, conn, cursor, **kwargs):
    data = request.get_json(force=True, silent=True) or {}

    content = data.get("content")
    if not isinstance(content, str) or not content.strip():
        return jsonify({"error": "Content is required"}), 400
    if len(content) > _MAX_CONTENT:
        return jsonify({"error": f"Content exceeds maximum length of {_MAX_CONTENT} characters"}), 400

    new_title = data.get("title")
    new_title = new_title.strip() if isinstance(new_title, str) and new_title.strip() else None
    if new_title and len(new_title) > _MAX_TITLE:
        return jsonify({"error": f"Title exceeds maximum length of {_MAX_TITLE} characters"}), 400

    # No pre-edit snapshot needed: the prior content is already the current
    # version row, so create_version below records this edit as the new
    # current version and the previous one remains restorable.
    try:
        cursor.execute(
            """UPDATE artifacts
               SET content = %s,
                   title = COALESCE(%s, title),
                   last_edited_by = 'user',
                   user_id = %s,
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = %s""",
            (content, new_title, user_id, artifact_id),
        )
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        return jsonify({"error": "An artifact with that title already exists"}), 409

    # New current version reflecting the manual edit.
    version = create_version(cursor, artifact_id, org_id, user_id, content, source="manual")
    conn.commit()

    record_audit_event(org_id, user_id, "update_artifact", "artifact", artifact_id, {}, request)
    return jsonify({"success": True, "version": version})


@artifact_bp.route("/api/artifacts/<artifact_id>", methods=["DELETE"])
@require_permission("artifacts", "write")
@with_artifact
def delete_artifact(user_id, artifact_id, *, org_id, conn, cursor, **kwargs):
    # Versions cascade via the artifact_versions FK (ON DELETE CASCADE).
    cursor.execute("DELETE FROM artifacts WHERE id = %s AND org_id = %s", (artifact_id, org_id))
    conn.commit()

    record_audit_event(org_id, user_id, "delete_artifact", "artifact", artifact_id, {}, request)
    return jsonify({"success": True})


@artifact_bp.route("/api/artifacts/<artifact_id>/versions", methods=["GET"])
@require_permission("artifacts", "read")
@with_artifact
def list_artifact_versions(user_id, artifact_id, *, org_id, conn, cursor, **kwargs):
    cursor.execute(
        """SELECT v.id, v.version_number, v.source, v.created_at,
                  v.generation_session_id, a.current_version_id
           FROM artifact_versions v
           JOIN artifacts a ON v.artifact_id = a.id
           WHERE a.id = %s AND a.org_id = %s
           ORDER BY v.version_number DESC""",
        (artifact_id, org_id),
    )
    rows = cursor.fetchall()

    current_version_id = str(rows[0][5]) if rows and rows[0][5] else None
    versions = [
        {
            "id": str(row[0]),
            "versionNumber": row[1],
            "source": row[2],
            "createdAt": iso_utc(row[3]),
            "generationSessionId": str(row[4]) if row[4] else None,
        }
        for row in rows
    ]
    return jsonify({"versions": versions, "currentVersionId": current_version_id})


@artifact_bp.route("/api/artifacts/<artifact_id>/versions/<version_id>", methods=["GET"])
@require_permission("artifacts", "read")
@with_artifact
def get_artifact_version(user_id, artifact_id, version_id, *, org_id, conn, cursor, **kwargs):
    if not is_valid_uuid(version_id):
        return jsonify({"error": "Invalid version ID"}), 400

    cursor.execute(
        """SELECT v.id, v.version_number, v.source, v.content, v.created_at
           FROM artifact_versions v
           JOIN artifacts a ON v.artifact_id = a.id
           WHERE v.id = %s AND a.id = %s AND a.org_id = %s""",
        (version_id, artifact_id, org_id),
    )
    row = cursor.fetchone()
    if not row:
        return jsonify({"error": "Version not found"}), 404

    return jsonify({
        "version": {
            "id": str(row[0]),
            "versionNumber": row[1],
            "source": row[2],
            "content": row[3],
            "createdAt": iso_utc(row[4]),
        }
    })


@artifact_bp.route("/api/artifacts/<artifact_id>/versions/<version_id>/restore", methods=["POST"])
@require_permission("artifacts", "write")
@with_artifact
def restore_artifact_version(user_id, artifact_id, version_id, *, org_id, conn, cursor, **kwargs):
    if not is_valid_uuid(version_id):
        return jsonify({"error": "Invalid version ID"}), 400

    cursor.execute(
        """SELECT v.content
           FROM artifact_versions v
           JOIN artifacts a ON v.artifact_id = a.id
           WHERE v.id = %s AND a.id = %s AND a.org_id = %s""",
        (version_id, artifact_id, org_id),
    )
    row = cursor.fetchone()
    if not row:
        return jsonify({"error": "Version not found"}), 404

    restored_content = row[0]
    cursor.execute(
        """UPDATE artifacts
           SET content = %s, current_version_id = %s,
               last_edited_by = 'user', updated_at = CURRENT_TIMESTAMP
           WHERE id = %s""",
        (restored_content, version_id, artifact_id),
    )
    conn.commit()

    record_audit_event(org_id, user_id, "restore_artifact_version", "artifact", artifact_id,
                       {"version_id": version_id}, request)
    return jsonify({"success": True, "content": restored_content})
