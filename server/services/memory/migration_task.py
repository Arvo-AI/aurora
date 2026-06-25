"""
Memory Data Migration Task

One-time Celery task to migrate existing data into the new memory system:
1. knowledge_base_memory → context memory entry
2. infrastructure_context → infrastructure memory entry
3. knowledge_base_documents (with content in S3) → runbook memory entries
"""

import io
import logging

from celery_worker import celery
from pypdf import PdfReader
from utils.db.connection_pool import db_pool
from utils.storage.storage import get_storage_manager
from services.artifacts.store import create_version
from services.memory.splitter import split_content, make_part_title, make_part_description

logger = logging.getLogger(__name__)


@celery.task(name="migrate_kb_to_memory", bind=True, max_retries=0)
def migrate_kb_to_memory(self):
    """Migrate all existing KB data into memory artifacts."""
    stats = {"context": 0, "infrastructure": 0, "documents": 0, "errors": 0}

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                # 1) Migrate knowledge_base_memory → context
                cursor.execute(
                    "SELECT org_id, user_id, content FROM knowledge_base_memory WHERE content IS NOT NULL AND content != ''"
                )
                kb_rows = cursor.fetchall()

                for org_id, user_id, content in kb_rows:
                    try:
                        cursor.execute(
                            """INSERT INTO artifacts
                                   (org_id, user_id, title, content, category, description,
                                    last_edited_by, updated_at)
                               VALUES (%s, %s, 'Org Context', %s, 'context',
                                       'User-provided org context (migrated from knowledge base)', 'user', CURRENT_TIMESTAMP)
                               ON CONFLICT (org_id, title) DO NOTHING
                               RETURNING id""",
                            (org_id, user_id, content),
                        )
                        row = cursor.fetchone()
                        if row:
                            create_version(cursor, str(row[0]), org_id, user_id, content, source="migration")
                            stats["context"] += 1
                    except Exception as e:
                        logger.warning(f"[Migration] Error migrating KB memory for org {org_id}: {e}")
                        stats["errors"] += 1
                        conn.rollback()

                # 2) Migrate infrastructure_context → infrastructure
                cursor.execute(
                    "SELECT org_id, user_id, content FROM infrastructure_context WHERE content IS NOT NULL AND content != ''"
                )
                infra_rows = cursor.fetchall()

                for org_id, user_id, content in infra_rows:
                    try:
                        cursor.execute(
                            """INSERT INTO artifacts
                                   (org_id, user_id, title, content, category, description,
                                    last_edited_by, updated_at)
                               VALUES (%s, %s, 'Infrastructure Context', %s, 'infrastructure',
                                       'Auto-discovered infrastructure topology (migrated)', 'system', CURRENT_TIMESTAMP)
                               ON CONFLICT (org_id, title) DO NOTHING
                               RETURNING id""",
                            (org_id, user_id, content),
                        )
                        row = cursor.fetchone()
                        if row:
                            create_version(cursor, str(row[0]), org_id, user_id, content, source="migration")
                            stats["infrastructure"] += 1
                    except Exception as e:
                        logger.warning(f"[Migration] Error migrating infra context for org {org_id}: {e}")
                        stats["errors"] += 1
                        conn.rollback()

                # 3) Migrate knowledge_base_documents (only if we can get content from S3)
                cursor.execute(
                    """SELECT org_id, user_id, original_filename, storage_path
                       FROM knowledge_base_documents
                       WHERE status = 'processed' AND storage_path IS NOT NULL"""
                )
                doc_rows = cursor.fetchall()

                for org_id, user_id, filename, storage_path in doc_rows:
                    try:
                        storage = get_storage_manager()
                        raw_bytes = storage.download_file(storage_path)

                        if not raw_bytes:
                            continue

                        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

                        if ext == "pdf":
                            reader = PdfReader(io.BytesIO(raw_bytes))
                            content = "\n\n".join(
                                f"[Page {i+1}]\n{p.extract_text()}"
                                for i, p in enumerate(reader.pages)
                                if p.extract_text()
                            )
                        else:
                            content = raw_bytes.decode("utf-8", errors="replace")

                        if not content.strip():
                            continue

                        base_title = filename.rsplit(".", 1)[0] if "." in filename else filename
                        base_desc = f"Migrated from uploaded document: {filename}"

                        # Split large documents into multiple parts
                        parts = split_content(content)
                        total_parts = len(parts)

                        for i, part_content in enumerate(parts, start=1):
                            if total_parts == 1:
                                title = base_title
                                desc = base_desc
                            else:
                                title = make_part_title(base_title, i, total_parts)
                                desc = make_part_description(base_desc, i, total_parts)

                            cursor.execute(
                                """INSERT INTO artifacts
                                       (org_id, user_id, title, content, category, description,
                                        last_edited_by, updated_at)
                                   VALUES (%s, %s, %s, %s, 'context',
                                           %s, 'user', CURRENT_TIMESTAMP)
                                   ON CONFLICT (org_id, title) DO NOTHING
                                   RETURNING id""",
                                (org_id, user_id, title, part_content, desc),
                            )
                            row = cursor.fetchone()
                            if row:
                                create_version(cursor, str(row[0]), org_id, user_id, part_content, source="migration")
                                stats["documents"] += 1
                    except Exception as e:
                        logger.warning(f"[Migration] Error migrating doc {filename} for org {org_id}: {e}")
                        stats["errors"] += 1
                        conn.rollback()

                conn.commit()

    except Exception as e:
        logger.error(f"[Migration] Fatal error: {e}")
        stats["errors"] += 1

    logger.info(f"[Migration] Complete: {stats}")
    return stats
