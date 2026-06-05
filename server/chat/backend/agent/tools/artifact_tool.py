"""
Artifact Tools

Agent-callable tools for listing, reading, and writing persistent markdown
documents (artifacts) that Aurora maintains over time. Available to every agent
surface (chat, scheduled Actions, background RCA, MCP) via get_cloud_tools().

Title-based — no UUIDs are ever exposed to the LLM. Each function resolves the
caller's org via set_rls_context() so writes are scoped to the right tenant even
outside a Flask request context (e.g. Celery action runs).
"""

import json
import logging

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_MAX_CONTENT = 100000
_MAX_TITLE = 500

_NO_USER_CTX = json.dumps({"error": "No user context available."})
_NO_ORG_CTX = json.dumps({"error": "No organization context available."})


class ListArtifactsArgs(BaseModel):
    """No args -- lists every artifact in the caller's workspace."""
    pass


class ReadArtifactArgs(BaseModel):
    title: str = Field(description="The exact title of the artifact to read")


class WriteArtifactArgs(BaseModel):
    title: str = Field(description="The exact title of the artifact to create or update")
    content: str = Field(description="The full markdown content of the document")


def list_artifacts(user_id: str | None = None, **kwargs) -> str:
    """List artifact metadata (title, version, last editor, updated time) for the org."""
    if not user_id:
        return _NO_USER_CTX

    try:
        from utils.db.connection_pool import db_pool
        from utils.auth.stateless_auth import set_rls_context

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                org_id = set_rls_context(cursor, conn, user_id, log_prefix="[ArtifactTool:list]")
                if not org_id:
                    return _NO_ORG_CTX

                cursor.execute(
                    """SELECT a.title, a.last_edited_by, a.updated_at,
                              COALESCE(v.version_number, 0)
                       FROM artifacts a
                       LEFT JOIN artifact_versions v ON a.current_version_id = v.id
                       WHERE a.org_id = %s
                       ORDER BY a.updated_at DESC""",
                    (org_id,),
                )
                rows = cursor.fetchall()

        artifacts = [
            {
                "title": row[0],
                "last_edited_by": row[1],
                "updated_at": row[2].isoformat() if row[2] else None,
                "version": row[3],
            }
            for row in rows
        ]
        return json.dumps({"status": "ok", "artifacts": artifacts})

    except Exception:
        logger.exception("[ArtifactTool] Failed to list artifacts")
        return json.dumps({"error": "Failed to list artifacts."})


def read_artifact(title: str, user_id: str | None = None, **kwargs) -> str:
    """Read one artifact's full markdown by exact title, or report it doesn't exist."""
    if not user_id:
        return _NO_USER_CTX

    if not title or not title.strip():
        return json.dumps({"error": "title is required."})

    title = title.strip()

    try:
        from utils.db.connection_pool import db_pool
        from utils.auth.stateless_auth import set_rls_context

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                org_id = set_rls_context(cursor, conn, user_id, log_prefix="[ArtifactTool:read]")
                if not org_id:
                    return _NO_ORG_CTX

                cursor.execute(
                    """SELECT a.content, a.last_edited_by, a.updated_at,
                              COALESCE(v.version_number, 0)
                       FROM artifacts a
                       LEFT JOIN artifact_versions v ON a.current_version_id = v.id
                       WHERE a.org_id = %s AND a.title = %s""",
                    (org_id, title),
                )
                row = cursor.fetchone()

        if not row:
            return json.dumps({
                "status": "not_found",
                "message": "No artifact with that title exists.",
            })

        return json.dumps({
            "status": "ok",
            "content": row[0] or "",
            "last_edited_by": row[1],
            "updated_at": row[2].isoformat() if row[2] else None,
            "version": row[3],
        })

    except Exception:
        logger.exception("[ArtifactTool] Failed to read artifact")
        return json.dumps({"error": "Failed to read artifact."})


def write_artifact(
    title: str,
    content: str,
    user_id: str | None = None,
    session_id: str | None = None,
    **kwargs,
) -> str:
    """Create or update an artifact by title, recording a new version each time."""
    if not user_id:
        return _NO_USER_CTX

    if not title or not title.strip():
        return json.dumps({"error": "title is required."})

    if len(title.strip()) > _MAX_TITLE:
        return json.dumps({"error": "Title exceeds maximum length (500 chars)."})

    if not content or not content.strip():
        return json.dumps({"error": "content cannot be empty."})

    if len(content) > _MAX_CONTENT:
        return json.dumps({"error": "Content exceeds maximum length (100000 chars)."})

    try:
        from utils.db.connection_pool import db_pool
        from utils.auth.stateless_auth import set_rls_context
        from services.artifacts.store import upsert_artifact_by_title

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                org_id = set_rls_context(cursor, conn, user_id, log_prefix="[ArtifactTool:write]")
                if not org_id:
                    return _NO_ORG_CTX

                _artifact_id, version = upsert_artifact_by_title(
                    cursor, org_id, user_id, title.strip(), content,
                    source="agent", session_id=session_id,
                )
                conn.commit()

        return json.dumps({
            "status": "ok",
            "message": f"Artifact saved (version {version}).",
            "version": version,
        })

    except Exception:
        logger.exception("[ArtifactTool] Failed to write artifact")
        return json.dumps({"error": "Failed to write artifact."})
