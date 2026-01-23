"""
Celery Tasks for Knowledge Base

Background tasks for document processing:
- Download from local filesystem
- Parse document
- Chunk content
- Store in Weaviate
"""

import logging

from celery_config import celery_app
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    name="knowledge_base.process_document",
)
def process_document(
    self,
    document_id: str,
    user_id: str,
    storage_path: str,
) -> None:
    """
    Process a document: download, parse, chunk, and store embeddings.

    Args:
        document_id: UUID of the document in knowledge_base_documents
        user_id: User identifier
        storage_path: Path to the file in local filesystem
    """
    logger.info(f"[KB Task] Processing document {document_id} for user {user_id}")

    try:
        # 1. Update status to 'processing'
        _update_document_status(document_id, user_id, "processing")

        # 2. Get document metadata
        doc_info = _get_document_info(document_id, user_id)
        if not doc_info:
            raise ValueError(f"Document {document_id} not found")

        original_filename = doc_info["original_filename"]
        file_type = doc_info["file_type"]

        logger.info(f"[KB Task] Reading {original_filename} from {storage_path}")

        # 3. Read file from local filesystem
        content = _download_file(storage_path)
        if not content:
            raise ValueError("Failed to read file from filesystem")

        logger.info(f"[KB Task] Downloaded {len(content)} bytes, parsing as {file_type}")

        # 4. Parse and chunk document
        from routes.knowledge_base.document_processor import DocumentProcessor

        processor = DocumentProcessor(user_id, document_id, original_filename)
        chunks = processor.process(content, file_type)

        if not chunks:
            raise ValueError("No content could be extracted from document")

        logger.info(f"[KB Task] Generated {len(chunks)} chunks")

        # 5. Store chunks in Weaviate
        from routes.knowledge_base.weaviate_client import insert_chunks

        inserted_count = insert_chunks(
            user_id=user_id,
            document_id=document_id,
            source_filename=original_filename,
            chunks=chunks,
        )

        logger.info(f"[KB Task] Stored {inserted_count} chunks in Weaviate")

        # 6. Update status to 'ready' with chunk count
        _update_document_status(
            document_id,
            user_id,
            "ready",
            chunk_count=inserted_count,
        )

        logger.info(f"[KB Task] Document {document_id} processed successfully")

    except Exception as exc:
        logger.exception(f"[KB Task] Error processing document {document_id}: {exc}")

        # Only set failed status on final retry attempt
        if self.request.retries >= self.max_retries:
            # Final attempt - mark as failed with generic user message
            _update_document_status(
                document_id,
                user_id,
                "failed",
                error_message="Document processing failed. Please try uploading again.",
            )
        else:
            # Retry with exponential backoff
            raise self.retry(exc=exc)


def _update_document_status(
    document_id: str,
    user_id: str,
    status: str,
    chunk_count: int | None = None,
    error_message: str | None = None,
) -> None:
    """Update document status in database."""
    try:
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()

            # Use static SQL with COALESCE to conditionally update fields
            cursor.execute(
                """
                UPDATE knowledge_base_documents
                SET status = %s,
                    chunk_count = COALESCE(%s, chunk_count),
                    error_message = COALESCE(%s, error_message),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s AND user_id = %s
                """,
                (status, chunk_count, error_message, document_id, user_id),
            )
            conn.commit()
            logger.info(f"[KB Task] Updated document {document_id} status to {status}")

    except Exception as e:
        logger.error(f"[KB Task] Error updating document status: {e}")
        raise  # Propagate to allow retry or alerting


def _get_document_info(document_id: str, user_id: str) -> dict | None:
    """Get document metadata from database.

    Returns:
        Document info dict if found, None if not found.

    Raises:
        Exception: Re-raises database errors to distinguish from "not found".
    """
    try:
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT original_filename, file_type, storage_path
                FROM knowledge_base_documents
                WHERE id = %s AND user_id = %s
                """,
                (document_id, user_id),
            )
            row = cursor.fetchone()

            if row:
                return {
                    "original_filename": row[0],
                    "file_type": row[1],
                    "storage_path": row[2],
                }
            return None

    except Exception as e:
        logger.error(f"[KB Task] Error getting document info: {e}")
        raise  # Re-raise to distinguish DB errors from "not found"


def _download_file(storage_path: str) -> bytes | None:
    """Download file content from storage."""
    try:
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
                # Remove users/{user_id}/ prefix to get relative path for download
                relative_path = "/".join(path_parts[2:]) if len(path_parts) > 2 else ""
            else:
                relative_path = path
        else:
            # Path doesn't have users/ prefix, use as-is
            relative_path = path
        
        storage = get_storage_manager(user_id=user_id)
        content = storage.download_bytes(relative_path, user_id=user_id)
        return content

    except Exception as e:
        logger.error(f"[KB Task] Error downloading file: {e}")
        return None


@celery_app.task(name="knowledge_base.cleanup_stale_documents")
def cleanup_stale_documents() -> dict:
    """
    Mark documents stuck in 'processing' or 'uploading' for >3 minutes as failed.

    This handles edge cases where:
    - Celery worker crashed mid-processing
    - DB update failed after max retries
    - Network issues caused silent failures
    """
    try:
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()

            # Find and mark stale documents as failed
            cursor.execute(
                """
                UPDATE knowledge_base_documents
                SET status = 'failed',
                    error_message = 'Processing timed out. Please try uploading again.',
                    updated_at = CURRENT_TIMESTAMP
                WHERE status IN ('processing', 'uploading')
                  AND updated_at < CURRENT_TIMESTAMP - INTERVAL '3 minutes'
                RETURNING id, user_id, original_filename
                """
            )
            stale_docs = cursor.fetchall()
            conn.commit()

            if stale_docs:
                for doc_id, user_id, filename in stale_docs:
                    logger.warning(
                        f"[KB Cleanup] Marked stale document as failed: {filename} "
                        f"(id={doc_id}, user={user_id})"
                    )

            logger.info(f"[KB Cleanup] Cleaned up {len(stale_docs)} stale documents")
            return {"cleaned": len(stale_docs)}

    except Exception as e:
        logger.error(f"[KB Cleanup] Error cleaning up stale documents: {e}")
        return {"error": str(e)}
