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
from utils.auth.stateless_auth import set_rls_context
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
                    cursor.execute("SAVEPOINT migrate_kb_row")
                    try:
                        set_rls_context(cursor, conn, user_id, log_prefix="[Migration:context]")
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
                        cursor.execute("RELEASE SAVEPOINT migrate_kb_row")
                    except Exception as e:
                        logger.warning("[Migration] Error migrating KB memory for org %s: %s", org_id, e)
                        stats["errors"] += 1
                        cursor.execute("ROLLBACK TO SAVEPOINT migrate_kb_row")

                # 2) Migrate infrastructure_context → infrastructure
                cursor.execute(
                    "SELECT org_id, user_id, content FROM infrastructure_context WHERE content IS NOT NULL AND content != ''"
                )
                infra_rows = cursor.fetchall()

                for org_id, user_id, content in infra_rows:
                    cursor.execute("SAVEPOINT migrate_infra_row")
                    try:
                        set_rls_context(cursor, conn, user_id, log_prefix="[Migration:infra]")
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
                        cursor.execute("RELEASE SAVEPOINT migrate_infra_row")
                    except Exception as e:
                        logger.warning("[Migration] Error migrating infra context for org %s: %s", org_id, e)
                        stats["errors"] += 1
                        cursor.execute("ROLLBACK TO SAVEPOINT migrate_infra_row")

                # 3) Migrate knowledge_base_documents (only if we can get content from S3)
                cursor.execute(
                    """SELECT org_id, user_id, original_filename, storage_path
                       FROM knowledge_base_documents
                       WHERE status = 'processed' AND storage_path IS NOT NULL"""
                )
                doc_rows = cursor.fetchall()

                for org_id, user_id, filename, storage_path in doc_rows:
                    cursor.execute("SAVEPOINT migrate_doc_row")
                    try:
                        storage = get_storage_manager()
                        raw_bytes = storage.download_file(storage_path)

                        if not raw_bytes:
                            cursor.execute("RELEASE SAVEPOINT migrate_doc_row")
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
                            cursor.execute("RELEASE SAVEPOINT migrate_doc_row")
                            continue

                        set_rls_context(cursor, conn, user_id, log_prefix="[Migration:docs]")

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
                                   VALUES (%s, %s, %s, %s, 'runbook',
                                           %s, 'user', CURRENT_TIMESTAMP)
                                   ON CONFLICT (org_id, title) DO NOTHING
                                   RETURNING id""",
                                (org_id, user_id, title, part_content, desc),
                            )
                            row = cursor.fetchone()
                            if row:
                                create_version(cursor, str(row[0]), org_id, user_id, part_content, source="migration")
                                stats["documents"] += 1

                        cursor.execute("RELEASE SAVEPOINT migrate_doc_row")
                    except Exception as e:
                        logger.warning("[Migration] Error migrating doc %s for org %s: %s", filename, org_id, e)
                        stats["errors"] += 1
                        cursor.execute("ROLLBACK TO SAVEPOINT migrate_doc_row")

                conn.commit()

    except Exception:
        logger.exception("[Migration] Fatal error")
        stats["errors"] += 1

    logger.info("[Migration] Complete: %s", stats)
    return stats
