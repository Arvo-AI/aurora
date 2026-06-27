"""Shared artifact persistence helpers.

Flask-free so the agent tool (title-based) and the REST routes (id + title)
share identical upsert + versioning logic, avoiding version-bump drift between
the two write paths. Every function operates on a caller-supplied cursor — the
caller owns the connection, RLS context, and commit/rollback.
"""

from typing import Optional, Tuple


def create_version(
    cursor,
    artifact_id: str,
    org_id: str,
    user_id: str,
    content: str,
    *,
    source: str,
    session_id: Optional[str] = None,
    set_current: bool = True,
) -> int:
    """Insert a new version row for an artifact and return its number.

    The next version number is computed inline via a subquery (MAX+1) at insert
    time. The artifact row is locked FOR UPDATE first so concurrent writers to
    the same artifact serialize here and can't both read the same MAX and
    collide on version_number. When set_current=True (default), also advances
    the artifact's current_version_id pointer.
    """
    # Serialize concurrent version allocation for this artifact (see docstring).
    cursor.execute("SELECT id FROM artifacts WHERE id = %s FOR UPDATE", (artifact_id,))

    cursor.execute(
        """INSERT INTO artifact_versions
           (artifact_id, org_id, user_id, content, version_number, source, generation_session_id)
           VALUES (%s, %s, %s, %s,
                   (SELECT COALESCE(MAX(version_number), 0) + 1
                    FROM artifact_versions WHERE artifact_id = %s),
                   %s, %s)
           RETURNING id, version_number""",
        (artifact_id, org_id, user_id, content, artifact_id, source, session_id),
    )
    row = cursor.fetchone()
    version_id, version_number = row[0], row[1]
    if set_current:
        cursor.execute(
            "UPDATE artifacts SET current_version_id = %s WHERE id = %s",
            (str(version_id), artifact_id),
        )
    return version_number


def upsert_artifact_by_title(
    cursor,
    org_id: str,
    user_id: str,
    title: str,
    content: str,
    *,
    source: str,
    session_id: Optional[str] = None,
) -> Tuple[str, int]:
    """Create or replace an artifact addressed by (org_id, title) and version it.

    Relies on the unique index idx_artifacts_org_title for an atomic upsert.
    last_edited_by is derived from source: a 'manual' write came from a human
    (the UI), anything else from the agent. Returns (artifact_id, version_number).
    """
    last_edited_by = "user" if source == "manual" else "agent"

    cursor.execute(
        """INSERT INTO artifacts (org_id, user_id, title, content, last_edited_by, updated_at)
           VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
           ON CONFLICT (org_id, title)
           DO UPDATE SET content = EXCLUDED.content,
                         user_id = EXCLUDED.user_id,
                         last_edited_by = EXCLUDED.last_edited_by,
                         updated_at = CURRENT_TIMESTAMP
           RETURNING id""",
        (org_id, user_id, title, content, last_edited_by),
    )
    row = cursor.fetchone()
    if not row:
        raise RuntimeError("Artifact upsert failed — access denied or conflict.")
    artifact_id = str(row[0])

    version_number = create_version(
        cursor, artifact_id, org_id, user_id, content,
        source=source, session_id=session_id, set_current=True,
    )
    return artifact_id, version_number
