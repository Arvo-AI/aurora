"""
Memory API Routes

REST endpoints for the org memory system. Memory entries live in the artifacts
table with category in (context, runbook, infrastructure, learned, postmortem).
Supports markdown and PDF upload (PDF → text extraction → markdown).
"""

import logging
import io

import psycopg2
from flask import Blueprint, jsonify, request
from pypdf import PdfReader

from utils.db.connection_pool import db_pool
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request, set_rls_context, get_user_display_name
from services.memory import MEMORY_CATEGORIES, USER_WRITABLE_CATEGORIES, SYSTEM_CATEGORY
from services.artifacts.store import create_version
from utils.validation import strip_nul

logger = logging.getLogger(__name__)

memory_bp = Blueprint("memory", __name__)

ALLOWED_EXTENSIONS = {"md", "txt", "pdf"}
MAX_CONTENT_LENGTH = 500_000  # 500KB per manually-created entry
MAX_UPLOAD_CONTENT_LENGTH = 50_000_000  # 50MB max extracted text per uploaded file
MAX_RAW_UPLOAD_BYTES = 100_000_000  # 100MB max raw file body to prevent OOM


def _extract_pdf_text(content: bytes) -> str:
    """Extract text from PDF bytes using pypdf."""
    pdf_reader = PdfReader(io.BytesIO(content))
    text_parts = []
    for page_num, page in enumerate(pdf_reader.pages):
        page_text = page.extract_text()
        if page_text and page_text.strip():
            text_parts.append(f"[Page {page_num + 1}]\n{page_text}")
    return "\n\n".join(text_parts)


@memory_bp.route("/entries", methods=["GET"])
@require_permission("memory", "read")
def list_entries(user_id):
    """List all memory entries for the org, optionally filtered by category."""
    org_id = get_org_id_from_request()
    category = request.args.get("category")

    if category and category not in MEMORY_CATEGORIES:
        return jsonify({"error": f"Invalid category. Must be one of: {', '.join(MEMORY_CATEGORIES)}"}), 400

    try:
        with db_pool.get_user_connection() as conn:
            cursor = conn.cursor()
            set_rls_context(cursor, conn, user_id, log_prefix="[Memory]")

            if category:
                cursor.execute(
                    """SELECT id, title, category, description, last_edited_by, last_edited_by_name, updated_at
                       FROM artifacts WHERE org_id = %s AND category = %s
                       ORDER BY updated_at DESC""",
                    (org_id, category),
                )
            else:
                cursor.execute(
                    """SELECT id, title, category, description, last_edited_by, last_edited_by_name, updated_at
                       FROM artifacts WHERE org_id = %s AND category = ANY(%s)
                       ORDER BY category, updated_at DESC""",
                    (org_id, list(MEMORY_CATEGORIES)),
                )
            rows = cursor.fetchall()

        entries = [
            {
                "id": str(row[0]),
                "title": row[1],
                "category": row[2],
                "description": row[3],
                "last_edited_by": row[4],
                "last_edited_by_name": row[5],
                "updated_at": row[6].isoformat() if row[6] else None,
            }
            for row in rows
        ]
        return jsonify({"entries": entries}), 200

    except Exception as e:
        logger.exception(f"[Memory] Error listing entries: {e}")
        return jsonify({"error": "Failed to list memory entries"}), 500


@memory_bp.route("/entries", methods=["POST"])
@require_permission("memory", "write")
def create_entry(user_id):
    """Create a new memory entry (JSON body with category, title, content)."""
    org_id = get_org_id_from_request()
    data = request.get_json(force=True, silent=True) or {}

    category = data.get("category", "").strip()
    title = data.get("title", "").strip()
    content = data.get("content", "").strip()
    description = data.get("description", "").strip()
    # When true, an existing entry with the same (category, title) is replaced.
    # When false/absent, a collision returns 409 so the UI can prompt the user.
    overwrite = bool(data.get("overwrite", False))

    # Users may only create into writable categories — "artifact" is
    # system-maintained (e.g. the Incident Index).
    if not category or category not in USER_WRITABLE_CATEGORIES:
        return jsonify({"error": f"category must be one of: {', '.join(USER_WRITABLE_CATEGORIES)}"}), 400
    if not title:
        return jsonify({"error": "title is required"}), 400
    if not content:
        return jsonify({"error": "content is required"}), 400
    content = strip_nul(content)
    if len(content) > MAX_CONTENT_LENGTH:
        return jsonify({"error": "Content exceeds 500KB limit"}), 400

    # Resolve the actual person so the entry can show "added by <name>" instead
    # of a generic "user"; None falls back to the generic label in the UI.
    editor_name = get_user_display_name(user_id)

    try:
        with db_pool.get_user_connection() as conn:
            cursor = conn.cursor()
            set_rls_context(cursor, conn, user_id, log_prefix="[Memory]")

            # Detect an existing entry with the same (category, title). Without an
            # explicit overwrite, surface a 409 so the UI can ask the user whether
            # to overwrite or keep both — never silently clobber existing content.
            if not overwrite:
                cursor.execute(
                    """SELECT id FROM artifacts
                       WHERE org_id = %s AND category = %s AND title = %s""",
                    (org_id, category, title),
                )
                if cursor.fetchone():
                    return jsonify({
                        "error": "A memory entry with this title already exists in that category",
                        "code": "conflict",
                    }), 409

            # Upsert: insert new, or (when overwrite=true) replace the existing entry.
            cursor.execute(
                """INSERT INTO artifacts
                       (org_id, user_id, title, content, category, description,
                        last_edited_by, last_edited_by_name, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, 'user', %s, CURRENT_TIMESTAMP)
                   ON CONFLICT (org_id, category, title)
                   DO UPDATE SET content = EXCLUDED.content,
                                 description = EXCLUDED.description,
                                 user_id = EXCLUDED.user_id,
                                 last_edited_by = 'user',
                                 last_edited_by_name = EXCLUDED.last_edited_by_name,
                                 updated_at = CURRENT_TIMESTAMP
                   RETURNING id""",
                (org_id, user_id, title, content, category, description or None, editor_name),
            )
            row = cursor.fetchone()
            artifact_id = str(row[0])

            version = create_version(
                cursor, artifact_id, org_id, user_id, content,
                source="manual", set_current=True,
            )
            conn.commit()

        return jsonify({"id": artifact_id, "version": version}), 201

    except Exception as e:
        logger.exception(f"[Memory] Error creating entry: {e}")
        return jsonify({"error": "Failed to create memory entry"}), 500


@memory_bp.route("/entries/<entry_id>", methods=["GET"])
@require_permission("memory", "read")
def get_entry(user_id, entry_id):
    """Get a single memory entry by ID."""
    org_id = get_org_id_from_request()

    try:
        with db_pool.get_user_connection() as conn:
            cursor = conn.cursor()
            set_rls_context(cursor, conn, user_id, log_prefix="[Memory]")

            cursor.execute(
                """SELECT id, title, category, description, content,
                          last_edited_by, last_edited_by_name, updated_at
                   FROM artifacts WHERE id = %s AND org_id = %s AND category = ANY(%s)""",
                (entry_id, org_id, list(MEMORY_CATEGORIES)),
            )
            row = cursor.fetchone()

        if not row:
            return jsonify({"error": "Memory entry not found"}), 404

        return jsonify({
            "id": str(row[0]),
            "title": row[1],
            "category": row[2],
            "description": row[3],
            "content": row[4],
            "last_edited_by": row[5],
            "last_edited_by_name": row[6],
            "updated_at": row[7].isoformat() if row[7] else None,
        }), 200

    except Exception as e:
        logger.exception(f"[Memory] Error getting entry: {e}")
        return jsonify({"error": "Failed to get memory entry"}), 500


@memory_bp.route("/entries/<entry_id>", methods=["DELETE"])
@require_permission("memory", "write")
def delete_entry(user_id, entry_id):
    """Delete a memory entry."""
    org_id = get_org_id_from_request()

    try:
        with db_pool.get_user_connection() as conn:
            cursor = conn.cursor()
            set_rls_context(cursor, conn, user_id, log_prefix="[Memory]")

            # Look up the category first so we can distinguish "not found" from
            # "reserved system entry" and return an accurate status/message.
            cursor.execute(
                "SELECT category FROM artifacts WHERE id = %s AND org_id = %s AND category = ANY(%s)",
                (entry_id, org_id, list(MEMORY_CATEGORIES)),
            )
            row = cursor.fetchone()
            if row is None:
                return jsonify({"error": "Memory entry not found"}), 404

            # System-maintained entries (e.g. the Incident Index) are read-only.
            if row[0] == SYSTEM_CATEGORY:
                return jsonify({"error": "This entry is system-managed and cannot be deleted."}), 403

            cursor.execute(
                "DELETE FROM artifacts WHERE id = %s AND org_id = %s AND category = ANY(%s)",
                (entry_id, org_id, list(USER_WRITABLE_CATEGORIES)),
            )
            if cursor.rowcount == 0:
                return jsonify({"error": "Memory entry not found"}), 404
            conn.commit()

        return jsonify({"success": True}), 200

    except Exception as e:
        logger.exception(f"[Memory] Error deleting entry: {e}")
        return jsonify({"error": "Failed to delete memory entry"}), 500


@memory_bp.route("/entries/<entry_id>", methods=["PUT"])
@require_permission("memory", "write")
def update_entry(user_id, entry_id):
    """Update a memory entry's editable fields (category, title, description, content).

    Any subset of fields may be provided. When content changes, a new version is
    recorded via create_version so edit history stays intact.
    """
    org_id = get_org_id_from_request()
    data = request.get_json(silent=True) or {}

    # Only build updates for fields the caller actually sent — everything is optional.
    has_category = "category" in data
    has_title = "title" in data
    has_description = "description" in data
    has_content = "content" in data

    if not any([has_category, has_title, has_description, has_content]):
        return jsonify({"error": "No updatable fields provided"}), 400

    category = data.get("category", "").strip() if has_category else None
    title = data.get("title", "").strip() if has_title else None
    description = data.get("description", "").strip() if has_description else None
    content = data.get("content", "") if has_content else None

    # When supplied, the target category must be user-writable — users can't move
    # entries into the system-maintained "artifact" category (e.g. Incident Index).
    if has_category and (not category or category not in USER_WRITABLE_CATEGORIES):
        return jsonify({"error": f"Invalid category. Must be one of: {', '.join(USER_WRITABLE_CATEGORIES)}"}), 400

    # Title cannot be blanked out.
    if has_title and not title:
        return jsonify({"error": "title cannot be empty"}), 400

    # Content, when edited, must be non-empty and within the manual-entry size cap.
    if has_content:
        content = strip_nul(content).strip()
        if not content:
            return jsonify({"error": "content cannot be empty"}), 400
        if len(content) > MAX_CONTENT_LENGTH:
            return jsonify({"error": "Content exceeds 500KB limit"}), 400

    try:
        with db_pool.get_user_connection() as conn:
            cursor = conn.cursor()
            set_rls_context(cursor, conn, user_id, log_prefix="[Memory]")

            # Look up the current category first so we can block editing a
            # system-managed entry (e.g. the Incident Index), which is read-only
            # for users and whose id-keyed lookups in services/memory must stay intact.
            cursor.execute(
                "SELECT category FROM artifacts WHERE id = %s AND org_id = %s AND category = ANY(%s)",
                (entry_id, org_id, list(MEMORY_CATEGORIES)),
            )
            existing = cursor.fetchone()
            if existing is None:
                return jsonify({"error": "Memory entry not found"}), 404

            # System-maintained entries are read-only for users.
            if existing[0] == SYSTEM_CATEGORY:
                return jsonify({"error": "This entry is system-managed and cannot be modified."}), 403

            # Stamp the actual editor's name (None → generic label in the UI).
            editor_name = get_user_display_name(user_id)

            # Assemble the SET clause dynamically from the provided fields.
            set_clauses = [
                "last_edited_by = 'user'",
                "last_edited_by_name = %s",
                "updated_at = CURRENT_TIMESTAMP",
            ]
            values = [editor_name]
            if has_category:
                set_clauses.append("category = %s")
                values.append(category)
            if has_title:
                set_clauses.append("title = %s")
                values.append(title)
            if has_description:
                set_clauses.append("description = %s")
                values.append(description or None)
            if has_content:
                set_clauses.append("content = %s")
                values.append(content)

            # Restrict the update to user-writable categories so a user-writable
            # entry can never be turned into (or overwrite) a system entry.
            values.extend([entry_id, org_id, list(USER_WRITABLE_CATEGORIES)])

            cursor.execute(
                f"""UPDATE artifacts
                    SET {', '.join(set_clauses)}
                    WHERE id = %s AND org_id = %s AND category = ANY(%s)
                    RETURNING id""",
                tuple(values),
            )
            row = cursor.fetchone()
            if row is None:
                return jsonify({"error": "Memory entry not found"}), 404

            # Record a new version whenever the content itself was edited.
            version = None
            if has_content:
                version = create_version(
                    cursor, str(row[0]), org_id, user_id, content,
                    source="manual", set_current=True,
                )

            conn.commit()

        result = {"success": True}
        if version is not None:
            result["version"] = version
        return jsonify(result), 200

    except psycopg2.errors.UniqueViolation:
        # Renaming/recategorizing collided with an existing (category, title) entry.
        return jsonify({"error": "A memory entry with this title already exists in that category"}), 409
    except Exception as e:
        logger.exception(f"[Memory] Error updating entry: {e}")
        return jsonify({"error": "Failed to update memory entry"}), 500


@memory_bp.route("/upload", methods=["POST"])
@require_permission("memory", "write")
def upload_file(user_id):
    """Upload a .md, .txt, or .pdf file as a memory entry."""
    org_id = get_org_id_from_request()

    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "No file selected"}), 400

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({"error": f"File type not allowed. Supported: {', '.join(ALLOWED_EXTENSIONS)}"}), 400

    category = request.form.get("category", "runbook").strip()
    if category not in USER_WRITABLE_CATEGORIES:
        return jsonify({"error": f"category must be one of: {', '.join(USER_WRITABLE_CATEGORIES)}"}), 400

    try:
        # Check Content-Length header to reject oversized uploads before reading
        content_length = request.content_length
        if content_length and content_length > MAX_RAW_UPLOAD_BYTES:
            return jsonify({"error": "File too large. Maximum raw upload size is 100MB."}), 400

        raw_bytes = file.read(MAX_RAW_UPLOAD_BYTES + 1)
        if len(raw_bytes) > MAX_RAW_UPLOAD_BYTES:
            return jsonify({"error": "File too large. Maximum raw upload size is 100MB."}), 400

        # Extract text based on file type
        if ext == "pdf":
            content = _extract_pdf_text(raw_bytes)
        else:
            content = raw_bytes.decode("utf-8", errors="replace")

        # Strip NUL bytes that Postgres text columns reject
        content = strip_nul(content)

        if not content.strip():
            return jsonify({"error": "No text content could be extracted from file"}), 400

        if len(content) > MAX_UPLOAD_CONTENT_LENGTH:
            return jsonify({"error": "Extracted text exceeds 50MB limit."}), 400

        # Use explicit title if provided, otherwise derive from filename
        base_title = request.form.get("title", "").strip()
        if not base_title:
            base_title = file.filename.rsplit(".", 1)[0] if "." in file.filename else file.filename
        description = request.form.get("description", "").strip()

        # Stamp the uploader's actual name (None → generic label in the UI).
        editor_name = get_user_display_name(user_id)

        with db_pool.get_user_connection() as conn:
            cursor = conn.cursor()
            set_rls_context(cursor, conn, user_id, log_prefix="[Memory]")

            cursor.execute(
                """INSERT INTO artifacts
                       (org_id, user_id, title, content, category, description,
                        last_edited_by, last_edited_by_name, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, 'user', %s, CURRENT_TIMESTAMP)
                   ON CONFLICT (org_id, category, title)
                   DO UPDATE SET content = EXCLUDED.content,
                                 description = EXCLUDED.description,
                                 user_id = EXCLUDED.user_id,
                                 last_edited_by = 'user',
                                 last_edited_by_name = EXCLUDED.last_edited_by_name,
                                 updated_at = CURRENT_TIMESTAMP
                   RETURNING id""",
                (org_id, user_id, base_title, content, category, description or None, editor_name),
            )
            row = cursor.fetchone()
            artifact_id = str(row[0])

            create_version(
                cursor, artifact_id, org_id, user_id, content,
                source="manual", set_current=True,
            )
            conn.commit()

        logger.info("[Memory] Uploaded file (%d chars)", len(content))
        return jsonify({"entries": [{"id": artifact_id, "title": base_title}], "parts": 1}), 201

    except Exception as e:
        logger.exception("[Memory] Error uploading file")
        return jsonify({"error": "Failed to upload file"}), 500
