"""
Memory Tools

Agent-callable tools for listing, reading, and writing org memory.
Memory lives in the same artifacts table but is scoped to memory categories
(context, runbook, infrastructure, learned, postmortem).

Reuses services/artifacts/store.py for persistence and versioning.
"""

import json
import logging
import re
from contextlib import contextmanager

from pydantic import BaseModel, Field

from services.memory import MEMORY_CATEGORIES
from services.artifacts.store import create_version
from utils.db.connection_pool import db_pool
from utils.auth.stateless_auth import set_rls_context

logger = logging.getLogger(__name__)

_MAX_CONTENT = 100000
_MAX_TITLE = 500
_GREP_MAX_RESULTS = 10
_GREP_SNIPPET_CHARS = 200

_NO_USER_CTX = json.dumps({"error": "No user context available."})
_NO_ORG_CTX = json.dumps({"error": "No organization context available."})


# ---------------------------------------------------------------------------
# Shared infrastructure
# ---------------------------------------------------------------------------


@contextmanager
def _memory_connection(user_id: str, operation: str):
    """Context manager that handles DB connection + RLS setup for memory ops.

    Yields (cursor, conn, org_id). Raises ValueError if user/org is missing.
    """
    if not user_id:
        raise ValueError("no_user")

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            org_id = set_rls_context(cursor, conn, user_id, log_prefix=f"[MemoryTool:{operation}]")
            if not org_id:
                raise ValueError("no_org")
            yield cursor, conn, org_id


def _validate_category(category: str, allow_empty: bool = False) -> str | None:
    """Validate category, return error JSON string if invalid, None if OK."""
    if allow_empty and not category:
        return None
    if not category or category not in MEMORY_CATEGORIES:
        return json.dumps({
            "error": f"Invalid category. Must be one of: {', '.join(MEMORY_CATEGORIES)}"
        })
    return None


def _validate_title(title: str) -> str | None:
    """Validate title, return error JSON string if invalid, None if OK."""
    if not title or not title.strip():
        return json.dumps({"error": "title is required."})
    if len(title.strip()) > _MAX_TITLE:
        return json.dumps({"error": "Title exceeds maximum length (500 chars)."})
    return None


def _error_response(e: ValueError) -> str:
    """Convert a ValueError from _memory_connection into a JSON response."""
    if str(e) == "no_user":
        return _NO_USER_CTX
    return _NO_ORG_CTX


# ---------------------------------------------------------------------------
# list_memories
# ---------------------------------------------------------------------------


class ListMemoriesArgs(BaseModel):
    category: str = Field(
        default="",
        description=(
            "Filter by category: context, runbook, infrastructure, learned, postmortem, artifact. "
            "Leave empty to list all memory entries."
        ),
    )


def list_memories(category: str = "", user_id: str | None = None, **kwargs) -> str:
    """List memory entries for the org, optionally filtered by category."""
    if err := _validate_category(category, allow_empty=True):
        return err

    try:
        with _memory_connection(user_id, "list") as (cursor, conn, org_id):
            # Filter to specific category or all memory categories
            if category:
                cat_filter = "category = %s"
                params = (org_id, category)
            else:
                cat_filter = "category = ANY(%s)"
                params = (org_id, list(MEMORY_CATEGORIES))

            cursor.execute(
                f"""SELECT title, category, description, last_edited_by, updated_at
                    FROM artifacts
                    WHERE org_id = %s AND {cat_filter}
                    ORDER BY category, updated_at DESC""",
                params,
            )
            rows = cursor.fetchall()

        entries = [
            {
                "title": row[0],
                "category": row[1],
                "description": row[2] or "",
                "last_edited_by": row[3],
                "updated_at": row[4].isoformat() if row[4] else None,
            }
            for row in rows
        ]
        return json.dumps({"status": "ok", "count": len(entries), "memories": entries})

    except ValueError as e:
        return _error_response(e)
    except Exception:
        logger.exception("[MemoryTool] Failed to list memories")
        return json.dumps({"error": "Failed to list memories."})


# ---------------------------------------------------------------------------
# read_memory
# ---------------------------------------------------------------------------


class ReadMemoryArgs(BaseModel):
    category: str = Field(description="The memory category (context, runbook, infrastructure, learned, postmortem, artifact)")
    title: str = Field(description="The exact title of the memory entry to read")


def read_memory(category: str, title: str, user_id: str | None = None, **kwargs) -> str:
    """Read one memory entry's full markdown by category and title."""
    if err := _validate_category(category):
        return err
    if err := _validate_title(title):
        return err

    try:
        with _memory_connection(user_id, "read") as (cursor, conn, org_id):
            cursor.execute(
                """SELECT content, description, last_edited_by, updated_at
                   FROM artifacts
                   WHERE org_id = %s AND category = %s AND title = %s""",
                (org_id, category, title.strip()),
            )
            row = cursor.fetchone()

        if not row:
            return json.dumps({
                "status": "not_found",
                "message": f"No memory entry '{title}' in category '{category}'.",
            })

        return json.dumps({
            "status": "ok",
            "category": category,
            "title": title.strip(),
            "content": row[0] or "",
            "description": row[1] or "",
            "last_edited_by": row[2],
            "updated_at": row[3].isoformat() if row[3] else None,
        })

    except ValueError as e:
        return _error_response(e)
    except Exception:
        logger.exception("[MemoryTool] Failed to read memory")
        return json.dumps({"error": "Failed to read memory."})


# ---------------------------------------------------------------------------
# write_memory
# ---------------------------------------------------------------------------


class WriteMemoryArgs(BaseModel):
    category: str = Field(description="The memory category (context, runbook, infrastructure, learned, postmortem)")
    title: str = Field(description="Short descriptive title for this memory entry")
    content: str = Field(description="The full markdown content of the memory entry")
    description: str = Field(
        default="",
        description="One-line summary for the memory index (helps with future retrieval)",
    )


def write_memory(
    category: str,
    title: str,
    content: str,
    description: str = "",
    user_id: str | None = None,
    session_id: str | None = None,
    **kwargs,
) -> str:
    """Create or update a memory entry. Records a new version each time."""
    if err := _validate_category(category):
        return err
    if err := _validate_title(title):
        return err
    if not content or not content.strip():
        return json.dumps({"error": "content cannot be empty."})
    if len(content) > _MAX_CONTENT:
        return json.dumps({"error": "Content exceeds maximum length (100000 chars)."})

    try:
        with _memory_connection(user_id, "write") as (cursor, conn, org_id):
            cursor.execute(
                """INSERT INTO artifacts
                       (org_id, user_id, title, content, category, description,
                        last_edited_by, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, 'agent', CURRENT_TIMESTAMP)
                   ON CONFLICT (org_id, title)
                   DO UPDATE SET content = EXCLUDED.content,
                                 category = EXCLUDED.category,
                                 description = EXCLUDED.description,
                                 user_id = EXCLUDED.user_id,
                                 last_edited_by = 'agent',
                                 updated_at = CURRENT_TIMESTAMP
                   RETURNING id""",
                (org_id, user_id, title.strip(), content,
                 category, description.strip() if description else None),
            )
            artifact_id = str(cursor.fetchone()[0])

            version = create_version(
                cursor, artifact_id, org_id, user_id, content,
                source="agent", session_id=session_id,
            )
            conn.commit()

        logger.info(f"[MemoryTool] Wrote memory '{category}/{title.strip()}' v{version} for org {org_id}")
        return json.dumps({
            "status": "ok",
            "message": f"Memory saved: {category}/{title.strip()} (version {version}).",
            "version": version,
        })

    except ValueError as e:
        return _error_response(e)
    except Exception:
        logger.exception("[MemoryTool] Failed to write memory")
        return json.dumps({"error": "Failed to write memory."})


# ---------------------------------------------------------------------------
# append_to_memory
# ---------------------------------------------------------------------------


class AppendToMemoryArgs(BaseModel):
    category: str = Field(description="The memory category (context, runbook, infrastructure, learned, postmortem)")
    title: str = Field(description="The exact title of the memory entry to append to")
    content: str = Field(description="Content to append at the end of the existing entry")


def append_to_memory(
    category: str,
    title: str,
    content: str,
    user_id: str | None = None,
    session_id: str | None = None,
    **kwargs,
) -> str:
    """Append content to an existing memory entry. Creates the entry if it doesn't exist."""
    if err := _validate_category(category):
        return err
    if err := _validate_title(title):
        return err
    if not content or not content.strip():
        return json.dumps({"error": "content cannot be empty."})

    try:
        with _memory_connection(user_id, "append") as (cursor, conn, org_id):
            cursor.execute(
                """SELECT id, content FROM artifacts
                   WHERE org_id = %s AND category = %s AND title = %s""",
                (org_id, category, title.strip()),
            )
            row = cursor.fetchone()

            if row:
                artifact_id = str(row[0])
                existing = row[1] or ""
                new_content = existing + "\n" + content if existing else content
            else:
                new_content = content
                artifact_id = None

            if len(new_content) > _MAX_CONTENT:
                return json.dumps({"error": "Resulting content would exceed 100KB limit."})

            if artifact_id:
                cursor.execute(
                    """UPDATE artifacts
                       SET content = %s, last_edited_by = 'agent', updated_at = CURRENT_TIMESTAMP
                       WHERE id = %s""",
                    (new_content, artifact_id),
                )
            else:
                cursor.execute(
                    """INSERT INTO artifacts
                           (org_id, user_id, title, content, category,
                            last_edited_by, updated_at)
                       VALUES (%s, %s, %s, %s, %s, 'agent', CURRENT_TIMESTAMP)
                       RETURNING id""",
                    (org_id, user_id, title.strip(), new_content, category),
                )
                artifact_id = str(cursor.fetchone()[0])

            version = create_version(
                cursor, artifact_id, org_id, user_id, new_content,
                source="agent", session_id=session_id,
            )
            conn.commit()

        logger.info(f"[MemoryTool] Appended to '{category}/{title.strip()}' v{version}")
        return json.dumps({
            "status": "ok",
            "message": f"Appended to {category}/{title.strip()} (version {version}).",
            "version": version,
        })

    except ValueError as e:
        return _error_response(e)
    except Exception:
        logger.exception("[MemoryTool] Failed to append to memory")
        return json.dumps({"error": "Failed to append to memory."})


# ---------------------------------------------------------------------------
# edit_memory
# ---------------------------------------------------------------------------


class EditMemoryArgs(BaseModel):
    category: str = Field(description="The memory category of the entry to edit")
    title: str = Field(description="The exact title of the memory entry to edit")
    old_text: str = Field(description="The exact text to find in the entry (must match precisely)")
    new_text: str = Field(description="The replacement text (use empty string to delete the matched section)")


def edit_memory(
    category: str,
    title: str,
    old_text: str,
    new_text: str,
    user_id: str | None = None,
    session_id: str | None = None,
    **kwargs,
) -> str:
    """Find and replace text within a memory entry. Like sed — surgical edits without rewriting."""
    if err := _validate_category(category):
        return err
    if err := _validate_title(title):
        return err
    if not old_text:
        return json.dumps({"error": "old_text is required (the text to find and replace)."})

    try:
        with _memory_connection(user_id, "edit") as (cursor, conn, org_id):
            cursor.execute(
                """SELECT id, content FROM artifacts
                   WHERE org_id = %s AND category = %s AND title = %s""",
                (org_id, category, title.strip()),
            )
            row = cursor.fetchone()

            if not row:
                return json.dumps({
                    "status": "not_found",
                    "message": f"No memory entry '{title}' in category '{category}'.",
                })

            artifact_id = str(row[0])
            existing = row[1] or ""

            if old_text not in existing:
                return json.dumps({
                    "status": "no_match",
                    "message": "old_text not found in the entry. Read the entry first to see its current content.",
                })

            new_content = existing.replace(old_text, new_text, 1)

            if len(new_content) > _MAX_CONTENT:
                return json.dumps({"error": "Resulting content would exceed 100KB limit."})

            cursor.execute(
                """UPDATE artifacts
                   SET content = %s, last_edited_by = 'agent', updated_at = CURRENT_TIMESTAMP
                   WHERE id = %s""",
                (new_content, artifact_id),
            )

            version = create_version(
                cursor, artifact_id, org_id, user_id, new_content,
                source="agent", session_id=session_id,
            )
            conn.commit()

        logger.info(f"[MemoryTool] Edited '{category}/{title.strip()}' v{version}")
        return json.dumps({
            "status": "ok",
            "message": f"Edited {category}/{title.strip()} (version {version}).",
            "version": version,
        })

    except ValueError as e:
        return _error_response(e)
    except Exception:
        logger.exception("[MemoryTool] Failed to edit memory")
        return json.dumps({"error": "Failed to edit memory."})


# ---------------------------------------------------------------------------
# grep_memories
# ---------------------------------------------------------------------------


class GrepMemoriesArgs(BaseModel):
    query: str = Field(description="Search pattern — supports regex (e.g. 'redis.*timeout', '(5xx|500)'). Plain strings work too.")
    category: str = Field(
        default="",
        description="Optionally limit search to a specific category",
    )


def grep_memories(
    query: str,
    category: str = "",
    user_id: str | None = None,
    **kwargs,
) -> str:
    """Search across all memory content using regex patterns. Like grep -ri across all entries."""
    if not query or not query.strip():
        return json.dumps({"error": "query is required."})
    if err := _validate_category(category, allow_empty=True):
        return err

    search_pattern = query.strip()

    try:
        with _memory_connection(user_id, "grep") as (cursor, conn, org_id):
            # Use PostgreSQL regex (~* = case-insensitive regex match)
            # Fall back to ILIKE if the pattern is invalid regex
            try:
                if category:
                    cursor.execute(
                        """SELECT title, category, content FROM artifacts
                           WHERE org_id = %s AND category = %s
                           AND content ~* %s
                           ORDER BY updated_at DESC
                           LIMIT %s""",
                        (org_id, category, search_pattern, _GREP_MAX_RESULTS),
                    )
                else:
                    cursor.execute(
                        """SELECT title, category, content FROM artifacts
                           WHERE org_id = %s AND category = ANY(%s)
                           AND content ~* %s
                           ORDER BY updated_at DESC
                           LIMIT %s""",
                        (org_id, list(MEMORY_CATEGORIES), search_pattern, _GREP_MAX_RESULTS),
                    )
                rows = cursor.fetchall()
            except Exception as regex_err:
                # Invalid regex — fall back to literal substring match
                conn.rollback()
                logger.debug(f"[MemoryTool:grep] Regex failed, falling back to ILIKE: {regex_err}")
                if category:
                    cursor.execute(
                        """SELECT title, category, content FROM artifacts
                           WHERE org_id = %s AND category = %s
                           AND content ILIKE %s
                           ORDER BY updated_at DESC
                           LIMIT %s""",
                        (org_id, category, f"%{search_pattern}%", _GREP_MAX_RESULTS),
                    )
                else:
                    cursor.execute(
                        """SELECT title, category, content FROM artifacts
                           WHERE org_id = %s AND category = ANY(%s)
                           AND content ILIKE %s
                           ORDER BY updated_at DESC
                           LIMIT %s""",
                        (org_id, list(MEMORY_CATEGORIES), f"%{search_pattern}%", _GREP_MAX_RESULTS),
                    )
                rows = cursor.fetchall()

        if not rows:
            return json.dumps({
                "status": "ok",
                "count": 0,
                "matches": [],
                "message": f"No matches for '{search_pattern}' in memory.",
            })

        matches = []
        for row_title, row_cat, row_content in rows:
            content = row_content or ""
            # Find match position for snippet extraction
            try:
                match = re.search(search_pattern, content, re.IGNORECASE)
                idx = match.start() if match else -1
                match_len = (match.end() - match.start()) if match else len(search_pattern)
            except re.error:
                idx = content.lower().find(search_pattern.lower())
                match_len = len(search_pattern)

            if idx >= 0:
                start = max(0, idx - _GREP_SNIPPET_CHARS // 2)
                end = min(len(content), idx + match_len + _GREP_SNIPPET_CHARS // 2)
                snippet = content[start:end]
                if start > 0:
                    snippet = "..." + snippet
                if end < len(content):
                    snippet = snippet + "..."
            else:
                snippet = content[:_GREP_SNIPPET_CHARS]

            matches.append({
                "title": row_title,
                "category": row_cat,
                "snippet": snippet,
            })

        return json.dumps({
            "status": "ok",
            "count": len(matches),
            "matches": matches,
        })

    except ValueError as e:
        return _error_response(e)
    except Exception:
        logger.exception("[MemoryTool] Failed to grep memories")
        return json.dumps({"error": "Failed to search memories."})
