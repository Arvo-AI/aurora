"""
Memory API Routes

REST endpoints for the org memory system. Memory entries live in the artifacts
table with category in (context, runbook, infrastructure, learned, postmortem).
Supports markdown and PDF upload (PDF → text extraction → markdown).
"""

import logging
import io

from flask import Blueprint, jsonify, request
from pypdf import PdfReader

from utils.db.connection_pool import db_pool
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request, set_rls_context
from services.memory import MEMORY_CATEGORIES
from services.memory.splitter import split_content, make_part_title, make_part_description
from services.artifacts.store import create_version

logger = logging.getLogger(__name__)

memory_bp = Blueprint("memory", __name__)

ALLOWED_EXTENSIONS = {"md", "txt", "pdf"}
MAX_CONTENT_LENGTH = 500_000  # 500KB per manually-created entry


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
                    """SELECT id, title, category, description, last_edited_by, updated_at
                       FROM artifacts WHERE org_id = %s AND category = %s
                       ORDER BY updated_at DESC""",
                    (org_id, category),
                )
            else:
                cursor.execute(
                    """SELECT id, title, category, description, last_edited_by, updated_at
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
                "updated_at": row[5].isoformat() if row[5] else None,
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

    if not category or category not in MEMORY_CATEGORIES:
        return jsonify({"error": f"category must be one of: {', '.join(MEMORY_CATEGORIES)}"}), 400
    if not title:
        return jsonify({"error": "title is required"}), 400
    if not content:
        return jsonify({"error": "content is required"}), 400
    if len(content) > MAX_CONTENT_LENGTH:
        return jsonify({"error": "Content exceeds 500KB limit"}), 400

    try:
        with db_pool.get_user_connection() as conn:
            cursor = conn.cursor()
            set_rls_context(cursor, conn, user_id, log_prefix="[Memory]")

            # Set category and description via direct insert with the upsert
            cursor.execute(
                """INSERT INTO artifacts
                       (org_id, user_id, title, content, category, description,
                        last_edited_by, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, 'user', CURRENT_TIMESTAMP)
                   ON CONFLICT (org_id, category, title)
                   DO UPDATE SET content = EXCLUDED.content,
                                 description = EXCLUDED.description,
                                 last_edited_by = 'user',
                                 updated_at = CURRENT_TIMESTAMP
                   RETURNING id""",
                (org_id, user_id, title, content, category, description or None),
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
                          last_edited_by, updated_at
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
            "updated_at": row[6].isoformat() if row[6] else None,
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

            cursor.execute(
                "DELETE FROM artifacts WHERE id = %s AND org_id = %s AND category = ANY(%s)",
                (entry_id, org_id, list(MEMORY_CATEGORIES)),
            )
            if cursor.rowcount == 0:
                return jsonify({"error": "Memory entry not found"}), 404
            conn.commit()

        return jsonify({"success": True}), 200

    except Exception as e:
        logger.exception(f"[Memory] Error deleting entry: {e}")
        return jsonify({"error": "Failed to delete memory entry"}), 500


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
    if category not in MEMORY_CATEGORIES:
        return jsonify({"error": f"category must be one of: {', '.join(MEMORY_CATEGORIES)}"}), 400

    try:
        raw_bytes = file.read()

        # Extract text based on file type
        if ext == "pdf":
            content = _extract_pdf_text(raw_bytes)
        else:
            content = raw_bytes.decode("utf-8", errors="replace")

        if not content.strip():
            return jsonify({"error": "No text content could be extracted from file"}), 400

        # Derive title from filename (without extension)
        base_title = file.filename.rsplit(".", 1)[0] if "." in file.filename else file.filename
        description = request.form.get("description", "").strip()

        # Split large documents into multiple parts at paragraph boundaries
        parts = split_content(content)
        total_parts = len(parts)
        created_entries = []

        with db_pool.get_user_connection() as conn:
            cursor = conn.cursor()
            set_rls_context(cursor, conn, user_id, log_prefix="[Memory]")

            for i, part_content in enumerate(parts, start=1):
                # Single-part documents keep their original title
                if total_parts == 1:
                    title = base_title
                    part_desc = description or None
                else:
                    title = make_part_title(base_title, i, total_parts)
                    part_desc = make_part_description(description, i, total_parts)

                cursor.execute(
                    """INSERT INTO artifacts
                           (org_id, user_id, title, content, category, description,
                            last_edited_by, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s, 'user', CURRENT_TIMESTAMP)
                       ON CONFLICT (org_id, category, title)
                       DO UPDATE SET content = EXCLUDED.content,
                                     description = EXCLUDED.description,
                                     last_edited_by = 'user',
                                     updated_at = CURRENT_TIMESTAMP
                       RETURNING id""",
                    (org_id, user_id, title, part_content, category, part_desc),
                )
                row = cursor.fetchone()
                artifact_id = str(row[0])

                create_version(
                    cursor, artifact_id, org_id, user_id, part_content,
                    source="manual", set_current=True,
                )
                created_entries.append({"id": artifact_id, "title": title})

            conn.commit()

        logger.info("[Memory] Uploaded file (%d parts)", total_parts)
        return jsonify({"entries": created_entries, "parts": total_parts}), 201

    except Exception as e:
        logger.exception("[Memory] Error uploading file")
        return jsonify({"error": "Failed to upload file"}), 500
