"""
Knowledge Base API Routes

Provides endpoints for:
- Memory: User-defined context always injected into agent prompts
- Documents: Upload/list/delete documents for RAG retrieval
"""

import logging
import os
import uuid
from typing import Any

from flask import Blueprint, jsonify, request
from werkzeug.utils import secure_filename

from utils.db.connection_pool import db_pool
from utils.web.cors_utils import create_cors_response
from utils.auth.stateless_auth import get_user_id_from_request

logger = logging.getLogger(__name__)

knowledge_base_bp = Blueprint("knowledge_base", __name__)

MEMORY_MAX_LENGTH = 5000
ALLOWED_EXTENSIONS = {'md', 'txt', 'pdf'}
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB
MAX_DOCUMENTS_PER_USER = 100
MAX_STORAGE_PER_USER_MB = 1000  # 1GB total

FILE_TYPE_MAP = {'md': 'markdown', 'pdf': 'pdf'}


def allowed_file(filename: str) -> bool:
    """Check if file extension is allowed."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def get_file_type(filename: str) -> str:
    """Get file type from filename extension."""
    ext = filename.rsplit('.', 1)[1].lower() if '.' in filename else ''
    return FILE_TYPE_MAP.get(ext, 'plaintext')


def serialize_document(row: tuple) -> dict[str, Any]:
    """Serialize a document row to a dictionary."""
    return {
        "id": str(row[0]),
        "filename": row[1],
        "original_filename": row[2],
        "file_type": row[3],
        "file_size_bytes": row[4],
        "status": row[5],
        "error_message": row[6],
        "chunk_count": row[7],
        "created_at": row[8].isoformat() if row[8] else None,
        "updated_at": row[9].isoformat() if row[9] else None,
    }


# =============================================================================
# Memory Endpoints
# =============================================================================

@knowledge_base_bp.route("/memory", methods=["GET", "OPTIONS"])
def get_memory():
    """Get user's knowledge base memory content."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "Authentication required"}), 401

    try:
        with db_pool.get_user_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
            conn.commit()

            cursor.execute(
                """
                SELECT content, updated_at
                FROM knowledge_base_memory
                WHERE user_id = %s
                """,
                (user_id,)
            )
            row = cursor.fetchone()

            if row:
                return jsonify({
                    "content": row[0],
                    "updated_at": row[1].isoformat() if row[1] else None
                }), 200
            else:
                return jsonify({
                    "content": "",
                    "updated_at": None
                }), 200

    except Exception as e:
        logger.exception(f"[KB] Error getting memory for user {user_id}: {e}")
        return jsonify({"error": "Failed to retrieve memory"}), 500


@knowledge_base_bp.route("/memory", methods=["PUT", "OPTIONS"])
def update_memory():
    """Update user's knowledge base memory content."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "Authentication required"}), 401

    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    content = data.get("content", "")

    # Validate content length
    if len(content) > MEMORY_MAX_LENGTH:
        return jsonify({
            "error": f"Content exceeds maximum length of {MEMORY_MAX_LENGTH} characters"
        }), 400

    try:
        with db_pool.get_user_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
            conn.commit()

            # Upsert the memory content
            cursor.execute(
                """
                INSERT INTO knowledge_base_memory (user_id, content, updated_at)
                VALUES (%s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (user_id)
                DO UPDATE SET content = EXCLUDED.content, updated_at = CURRENT_TIMESTAMP
                RETURNING updated_at
                """,
                (user_id, content)
            )
            row = cursor.fetchone()
            conn.commit()

            logger.info(f"[KB] Updated memory for user {user_id} ({len(content)} chars)")

            return jsonify({
                "success": True,
                "content": content,
                "updated_at": row[0].isoformat() if row and row[0] else None
            }), 200

    except Exception as e:
        logger.exception(f"[KB] Error updating memory for user {user_id}: {e}")
        return jsonify({"error": "Failed to update memory"}), 500


# =============================================================================
# Document Endpoints
# =============================================================================

@knowledge_base_bp.route("/documents", methods=["GET", "OPTIONS"])
def list_documents():
    """List all documents for the user."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "Authentication required"}), 401

    try:
        with db_pool.get_user_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
            conn.commit()

            cursor.execute(
                """
                SELECT id, filename, original_filename, file_type, file_size_bytes,
                       status, error_message, chunk_count, created_at, updated_at
                FROM knowledge_base_documents
                WHERE user_id = %s
                ORDER BY created_at DESC
                """,
                (user_id,)
            )
            documents = [serialize_document(row) for row in cursor.fetchall()]

            # Calculate usage stats
            total_bytes = sum(d.get("file_size_bytes", 0) for d in documents)
            usage = {
                "document_count": len(documents),
                "document_limit": MAX_DOCUMENTS_PER_USER,
                "storage_used_mb": round(total_bytes / (1024 * 1024), 2),
                "storage_limit_mb": MAX_STORAGE_PER_USER_MB,
            }

            return jsonify({"documents": documents, "usage": usage}), 200

    except Exception as e:
        logger.exception(f"[KB] Error listing documents for user {user_id}: {e}")
        return jsonify({"error": "Failed to list documents"}), 500


@knowledge_base_bp.route("/upload", methods=["POST", "OPTIONS"])
def upload_document():
    """Upload a new document for processing."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "Authentication required"}), 401

    # Check if file is in request
    if 'file' not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files['file']

    if file.filename == '':
        return jsonify({"error": "No file selected"}), 400

    if not allowed_file(file.filename):
        return jsonify({
            "error": f"File type not allowed. Supported types: {', '.join(ALLOWED_EXTENSIONS)}"
        }), 400

    # Check file size
    file.seek(0, os.SEEK_END)
    file_size = file.tell()
    file.seek(0)

    if file_size > MAX_FILE_SIZE:
        return jsonify({
            "error": f"File too large. Maximum size is {MAX_FILE_SIZE // (1024*1024)}MB"
        }), 400

    # Check user limits (document count and total storage)
    limit_error = _check_user_limits(user_id, file_size)
    if limit_error:
        return jsonify({"error": limit_error}), 400

    original_filename = secure_filename(file.filename)
    doc_id = str(uuid.uuid4())

    # Validate secure_filename result - it may be empty or lose extension
    if not original_filename or '.' not in original_filename:
        # Try to extract extension from raw filename and reconstruct
        raw_ext = ''
        if file.filename and '.' in file.filename:
            raw_ext = file.filename.rsplit('.', 1)[1].lower()
        if raw_ext in ALLOWED_EXTENSIONS:
            original_filename = f"{doc_id}.{raw_ext}"
        else:
            return jsonify({
                "error": "Invalid filename. Please use a file with a valid extension (.md, .txt, .pdf)."
            }), 400

    file_type = get_file_type(original_filename)

    # Generate a unique filename to avoid collisions
    filename = f"{doc_id}_{original_filename}"

    try:
        with db_pool.get_user_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
            conn.commit()

            # Check if document with same original filename exists
            cursor.execute(
                """
                SELECT id FROM knowledge_base_documents
                WHERE user_id = %s AND original_filename = %s
                """,
                (user_id, original_filename)
            )
            existing = cursor.fetchone()
            if existing:
                return jsonify({
                    "error": f"Document '{original_filename}' already exists. Delete it first to re-upload."
                }), 409

            # Create document record with 'uploading' status
            cursor.execute(
                """
                INSERT INTO knowledge_base_documents
                (id, user_id, filename, original_filename, file_type, file_size_bytes, status)
                VALUES (%s, %s, %s, %s, %s, %s, 'uploading')
                RETURNING id, created_at
                """,
                (doc_id, user_id, filename, original_filename, file_type, file_size)
            )
            row = cursor.fetchone()
            conn.commit()

            logger.info(f"[KB] Created document record {doc_id} for user {user_id}: {original_filename}")

        # Store file to local filesystem and trigger processing task
        storage_path = None
        try:
            storage_path = _upload_file(user_id, doc_id, filename, file)

            # Update document with storage path and set status to processing
            try:
                with db_pool.get_user_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
                    conn.commit()

                    cursor.execute(
                        """
                        UPDATE knowledge_base_documents
                        SET storage_path = %s, status = 'processing', updated_at = CURRENT_TIMESTAMP
                        WHERE id = %s AND user_id = %s
                        """,
                        (storage_path, doc_id, user_id)
                    )
                    conn.commit()
            except Exception as db_error:
                # DB update failed after file upload - clean up orphan file
                logger.exception(f"[KB] DB update failed after file upload: {db_error}")
                try:
                    _delete_file(storage_path)
                    logger.info(f"[KB] Cleaned up orphan file: {storage_path}")
                except Exception as cleanup_error:
                    logger.warning(f"[KB] Failed to clean up file {storage_path}: {cleanup_error}")
                raise  # Re-raise to hit outer except

            # Trigger Celery task for document processing
            from routes.knowledge_base.tasks import process_document
            process_document.delay(doc_id, user_id, storage_path)

            logger.info(f"[KB] Uploaded document {doc_id} to {storage_path}, triggered processing")

        except Exception as upload_error:
            logger.exception(f"[KB] Failed to upload document {doc_id}: {upload_error}")
            # Update status to failed - use generic message for users, log full error server-side
            with db_pool.get_user_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
                conn.commit()

                cursor.execute(
                    """
                    UPDATE knowledge_base_documents
                    SET status = 'failed', error_message = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s AND user_id = %s
                    """,
                    ("Document upload failed. Please try again.", doc_id, user_id)
                )
                conn.commit()

            return jsonify({"error": "Failed to upload document"}), 500

        return jsonify({
            "id": doc_id,
            "filename": filename,
            "original_filename": original_filename,
            "file_type": file_type,
            "file_size_bytes": file_size,
            "status": "processing",
            "created_at": row[1].isoformat() if row and row[1] else None
        }), 201

    except Exception as e:
        logger.exception(f"[KB] Error uploading document for user {user_id}: {e}")
        return jsonify({"error": "Failed to upload document"}), 500


@knowledge_base_bp.route("/documents/<doc_id>", methods=["GET", "OPTIONS"])
def get_document(doc_id: str):
    """Get a specific document's details."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "Authentication required"}), 401

    try:
        with db_pool.get_user_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
            conn.commit()

            cursor.execute(
                """
                SELECT id, filename, original_filename, file_type, file_size_bytes,
                       status, error_message, chunk_count, created_at, updated_at
                FROM knowledge_base_documents
                WHERE id = %s AND user_id = %s
                """,
                (doc_id, user_id)
            )
            row = cursor.fetchone()

            if not row:
                return jsonify({"error": "Document not found"}), 404

            return jsonify(serialize_document(row)), 200

    except Exception as e:
        logger.exception(f"[KB] Error getting document {doc_id} for user {user_id}: {e}")
        return jsonify({"error": "Failed to get document"}), 500


@knowledge_base_bp.route("/documents/<doc_id>", methods=["DELETE", "OPTIONS"])
def delete_document(doc_id: str):
    """Delete a document and its chunks."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "Authentication required"}), 401

    try:
        with db_pool.get_user_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
            conn.commit()

            # Get document info first
            cursor.execute(
                """
                SELECT storage_path, original_filename
                FROM knowledge_base_documents
                WHERE id = %s AND user_id = %s
                """,
                (doc_id, user_id)
            )
            row = cursor.fetchone()

            if not row:
                return jsonify({"error": "Document not found"}), 404

            storage_path = row[0]
            original_filename = row[1]

            # Delete from Weaviate first
            try:
                from routes.knowledge_base.weaviate_client import delete_document_chunks
                delete_document_chunks(user_id, doc_id)
                logger.info(f"[KB] Deleted Weaviate chunks for document {doc_id}")
            except Exception as weaviate_error:
                logger.warning(f"[KB] Failed to delete Weaviate chunks for {doc_id}: {weaviate_error}")

            # Delete from local filesystem
            if storage_path:
                try:
                    _delete_file(storage_path)
                    logger.info(f"[KB] Deleted file {storage_path}")
                except Exception as file_error:
                    logger.warning(f"[KB] Failed to delete file {storage_path}: {file_error}")

            # Delete from database
            cursor.execute(
                """
                DELETE FROM knowledge_base_documents
                WHERE id = %s AND user_id = %s
                """,
                (doc_id, user_id)
            )
            conn.commit()

            logger.info(f"[KB] Deleted document {doc_id} ({original_filename}) for user {user_id}")

            return jsonify({"success": True}), 200

    except Exception as e:
        logger.exception(f"[KB] Error deleting document {doc_id} for user {user_id}: {e}")
        return jsonify({"error": "Failed to delete document"}), 500


@knowledge_base_bp.route("/search", methods=["POST", "OPTIONS"])
def search_documents():
    """Search the knowledge base (for direct API usage, not agent tool)."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "Authentication required"}), 401

    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    query = data.get("query", "")
    limit = data.get("limit", 5)

    if not query:
        return jsonify({"error": "Query is required"}), 400

    if not isinstance(limit, int) or limit < 1 or limit > 20:
        limit = 5

    try:
        from routes.knowledge_base.weaviate_client import search_knowledge_base
        results = search_knowledge_base(user_id, query, limit)

        return jsonify({
            "query": query,
            "results": results
        }), 200

    except Exception as e:
        logger.exception(f"[KB] Error searching for user {user_id}: {e}")
        return jsonify({"error": "Failed to search knowledge base"}), 500


# =============================================================================
# Helper Functions
# =============================================================================

def _upload_file(user_id: str, doc_id: str, filename: str, file) -> str:
    """Upload file to storage and return the storage URI."""
    from utils.storage.storage import get_storage_manager
    
    storage = get_storage_manager(user_id=user_id)
    storage_path = f"knowledge-base/{doc_id}/{filename}"
    
    # Upload file to storage (SeaWeedFS via S3-compatible API)
    storage_uri = storage.upload_file(file, storage_path, user_id=user_id)
    
    return storage_uri


def _delete_file(storage_path: str) -> None:
    """Delete file from storage."""
    from utils.storage.storage import get_storage_manager
    
    # Extract path and user_id from URI or path
    user_id = None
    path = storage_path
    
    # If it's an s3:// URI, extract the path
    if storage_path.startswith("s3://"):
        # Extract path: s3://bucket/users/{user_id}/knowledge-base/... -> users/{user_id}/knowledge-base/...
        parts = storage_path[5:].split("/", 1)
        if len(parts) > 1:
            path = parts[1]
    
    # Extract user_id from path (format: users/{user_id}/knowledge-base/...)
    if path.startswith("users/"):
        path_parts = path.split("/")
        if len(path_parts) > 1:
            user_id = path_parts[1]
            # Remove users/{user_id}/ prefix to get relative path for delete
            relative_path = "/".join(path_parts[2:]) if len(path_parts) > 2 else ""
        else:
            relative_path = path
    else:
        # Path doesn't have users/ prefix, use as-is
        relative_path = path
    
    storage = get_storage_manager(user_id=user_id)
    storage.delete_file(relative_path, user_id=user_id)


def _check_user_limits(user_id: str, new_file_size: int) -> str | None:
    """
    Check if user has exceeded document or storage limits.

    Returns error message if limit exceeded, None if OK.
    """
    try:
        with db_pool.get_user_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SET myapp.current_user_id = %s;", (user_id,))

            # Get document count and total storage for user
            cursor.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(file_size_bytes), 0)
                FROM knowledge_base_documents
                WHERE user_id = %s
                """,
                (user_id,)
            )
            doc_count, total_bytes = cursor.fetchone()

            # Check document count limit
            if doc_count >= MAX_DOCUMENTS_PER_USER:
                return f"Document limit reached ({MAX_DOCUMENTS_PER_USER} documents). Delete some documents to upload more."

            # Check storage limit (convert to MB for comparison)
            # Convert total_bytes to float to avoid Decimal + float type mismatch
            total_mb = float(total_bytes) / (1024 * 1024)
            new_file_mb = new_file_size / (1024 * 1024)

            if total_mb + new_file_mb > MAX_STORAGE_PER_USER_MB:
                return f"Storage limit reached ({MAX_STORAGE_PER_USER_MB}MB). Delete some documents to free up space."

            return None

    except Exception as e:
        logger.error(f"[KB] Error checking user limits: {e}")
        # Don't block upload on limit check failure
        return None
