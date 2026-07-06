"""
Postmortem Tools

Agent-callable tools for reading and writing postmortem documents.
Postmortems are stored as artifacts with category='postmortem' and an
incident_id linking them to the originating incident.
"""

import json
import logging

from pydantic import BaseModel, Field

from utils.validation import is_valid_uuid, strip_nul
from utils.db.connection_pool import db_pool
from utils.auth.stateless_auth import set_rls_context
from services.artifacts.store import create_version

logger = logging.getLogger(__name__)


class GetPostmortemArgs(BaseModel):
    incident_id: str = Field(description="The UUID of the incident to retrieve the postmortem for")


class SavePostmortemArgs(BaseModel):
    incident_id: str = Field(description="The UUID of the incident to save the postmortem for")
    content: str = Field(description="The full markdown content of the postmortem document")


def get_postmortem(
    incident_id: str,
    user_id: str | None = None,
    **kwargs,
) -> str:
    """Read the current postmortem for an incident. Returns the markdown content
    or an error if no postmortem exists yet."""
    if not user_id:
        return json.dumps({"error": "No user context available."})

    if not incident_id:
        return json.dumps({"error": "incident_id is required."})

    if not is_valid_uuid(incident_id):
        logger.warning(
            "[PostmortemTool] get_postmortem called with non-UUID incident_id=%r — rejecting",
            incident_id,
        )
        return json.dumps({
            "error": (
                "incident_id must be the Aurora internal UUID (e.g. the id from the "
                "incidents table).  You may have passed an external identifier such as "
                "an incident.io ULID.  Check the incident context for the correct UUID."
            )
        })

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix="[PostmortemTool]")
                cursor.execute(
                    """SELECT content, created_at, updated_at
                       FROM artifacts
                       WHERE incident_id = %s AND category = 'postmortem'""",
                    (incident_id,),
                )
                row = cursor.fetchone()

        if not row:
            return json.dumps({
                "status": "not_found",
                "message": "No postmortem exists for this incident yet.",
            })

        return json.dumps({
            "status": "ok",
            "content": row[0] or "",
            "generated_at": row[1].isoformat() if row[1] else None,
            "updated_at": row[2].isoformat() if row[2] else None,
        })

    except Exception:
        logger.exception("[PostmortemTool] Failed to get postmortem for %s", incident_id)
        return json.dumps({"error": "Failed to retrieve postmortem."})


def save_postmortem(
    incident_id: str,
    content: str,
    user_id: str | None = None,
    session_id: str | None = None,
    **kwargs,
) -> str:
    """Save or update a postmortem for an incident. Creates a new version
    each time it is called. The content should be complete markdown."""
    if not user_id:
        return json.dumps({"error": "No user context available."})

    if not incident_id:
        return json.dumps({"error": "incident_id is required."})

    if not is_valid_uuid(incident_id):
        logger.warning(
            "[PostmortemTool] save_postmortem called with non-UUID incident_id=%r — rejecting",
            incident_id,
        )
        return json.dumps({
            "error": (
                "incident_id must be the Aurora internal UUID (e.g. the id from the "
                "incidents table).  You may have passed an external identifier such as "
                "an incident.io ULID or a descriptive string.  Check the incident "
                "context for the correct UUID before retrying."
            )
        })

    if not content or not content.strip():
        return json.dumps({"error": "content cannot be empty."})

    if len(content) > 100000:
        return json.dumps({"error": "Content exceeds maximum length (100000 chars)."})

    content = strip_nul(content)

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix="[PostmortemTool:save]")

                # Resolve org_id and alert_title from the incident
                cursor.execute(
                    "SELECT org_id, alert_title FROM incidents WHERE id = %s",
                    (incident_id,),
                )
                incident_row = cursor.fetchone()
                if not incident_row:
                    return json.dumps({"error": "Incident not found or not accessible."})

                org_id = incident_row[0]
                alert_title = incident_row[1]
                short_id = str(incident_id)[:8]
                title = f"Postmortem: {alert_title} [{short_id}]" if alert_title else f"Postmortem ({incident_id})"

                # Upsert the artifact with incident_id linkage
                cursor.execute(
                    """INSERT INTO artifacts
                           (org_id, user_id, title, content, category, description,
                            incident_id, generation_session_id, last_edited_by, updated_at)
                       VALUES (%s, %s, %s, %s, 'postmortem', %s,
                               %s, %s, 'agent', CURRENT_TIMESTAMP)
                       ON CONFLICT (org_id, category, title)
                       DO UPDATE SET content = EXCLUDED.content,
                                     generation_session_id = EXCLUDED.generation_session_id,
                                     last_edited_by = 'agent',
                                     updated_at = CURRENT_TIMESTAMP
                       RETURNING id""",
                    (org_id, user_id, title, content,
                     f"Postmortem for incident: {alert_title or incident_id}",
                     incident_id, session_id),
                )
                row = cursor.fetchone()
                if not row:
                    conn.rollback()
                    return json.dumps({"error": "Failed to save postmortem — access denied or conflict."})
                artifact_id = str(row[0])

                version = create_version(
                    cursor, artifact_id, org_id, user_id, content,
                    source="agent", session_id=session_id,
                )
                conn.commit()

        return json.dumps({
            "status": "ok",
            "message": f"Postmortem saved (version {version}).",
            "version": version,
        })

    except Exception:
        logger.exception("[PostmortemTool] Failed to save postmortem for %s", incident_id)
        return json.dumps({"error": "Failed to save postmortem."})
