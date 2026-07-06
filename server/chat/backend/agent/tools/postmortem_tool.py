"""
Postmortem Tools

Agent-callable tools for reading and writing postmortem documents.
Used by the built-in "Generate Postmortem" action and available
during regular chat for postmortem-related queries.
"""

import json
import logging

from pydantic import BaseModel, Field

from utils.validation import is_valid_uuid

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
        from utils.db.connection_pool import db_pool
        from utils.auth.stateless_auth import set_rls_context

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix="[PostmortemTool]")
                cursor.execute(
                    """SELECT content, generated_at, updated_at
                       FROM postmortems
                       WHERE incident_id = %s""",
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

    try:
        from utils.db.connection_pool import db_pool
        from utils.auth.stateless_auth import set_rls_context

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix="[PostmortemTool:save]")

                # Resolve org_id from the incident (under RLS) to prevent cross-tenant writes
                cursor.execute("SELECT org_id FROM incidents WHERE id = %s", (incident_id,))
                incident_row = cursor.fetchone()
                org_id = incident_row[0] if incident_row else None

                if not org_id:
                    return json.dumps({"error": "Incident not found or not accessible."})

                # Upsert the postmortem
                cursor.execute(
                    """INSERT INTO postmortems (incident_id, user_id, org_id, content,
                                               generation_session_id, generated_at, updated_at)
                       VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                       ON CONFLICT (incident_id)
                       DO UPDATE SET content = EXCLUDED.content,
                                     generation_session_id = EXCLUDED.generation_session_id,
                                     updated_at = CURRENT_TIMESTAMP
                       RETURNING id""",
                    (incident_id, user_id, org_id, content, session_id),
                )
                postmortem_row = cursor.fetchone()
                if not postmortem_row:
                    conn.rollback()
                    return json.dumps({"error": "Failed to save postmortem — access denied or conflict."})
                postmortem_id = str(postmortem_row[0])

                # Create a version row (atomic with a subquery to prevent race conditions)
                cursor.execute(
                    """INSERT INTO postmortem_versions
                       (postmortem_id, org_id, user_id, content, version_number, source, generation_session_id)
                       VALUES (%s, %s, %s, %s,
                               (SELECT COALESCE(MAX(version_number), 0) + 1
                                FROM postmortem_versions WHERE postmortem_id = %s),
                               %s, %s)
                       RETURNING id, version_number""",
                    (postmortem_id, org_id, user_id, content, postmortem_id, "agent", session_id),
                )
                version_row = cursor.fetchone()
                version_id, next_version = str(version_row[0]), version_row[1]

                # Update the current version pointer
                cursor.execute(
                    "UPDATE postmortems SET current_version_id = %s WHERE id = %s",
                    (version_id, postmortem_id),
                )

                conn.commit()

        return json.dumps({
            "status": "ok",
            "message": f"Postmortem saved (version {next_version}).",
            "version": next_version,
        })

    except Exception:
        logger.exception("[PostmortemTool] Failed to save postmortem for %s", incident_id)
        return json.dumps({"error": "Failed to save postmortem."})
