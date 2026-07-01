"""
Memory Data Migration Task

One-time Celery task to migrate existing data into the new memory system:
1. knowledge_base_memory → context memory entry
2. infrastructure_context → infrastructure memory entry
3. knowledge_base_documents (with content in S3) → runbook memory entries
"""

import io
import logging

from celery_config import celery_app
from pypdf import PdfReader
from utils.db.connection_pool import db_pool
from utils.storage.storage import get_storage_manager
from services.artifacts.store import create_version
from services.memory.splitter import split_content, make_part_title, make_part_description

logger = logging.getLogger(__name__)


def _normalize_storage_path(storage_path: str) -> str:
    """Strip s3://bucket/ prefix from legacy KB document paths."""
    if storage_path.startswith("s3://"):
        parts = storage_path[5:].split("/", 1)
        if len(parts) > 1:
            return parts[1]
    return storage_path


def _resolve_user_for_org(cursor, org_id: str) -> str | None:
    """Pick any user in the org so RLS context can be set during migration."""
    cursor.execute(
        "SELECT id FROM users WHERE org_id = %s ORDER BY id LIMIT 1",
        (org_id,),
    )
    row = cursor.fetchone()
    return row[0] if row else None


def _set_rls_for_row(cursor, org_id: str, user_id: str) -> None:
    """Set RLS session vars without committing (set_rls_context commits and breaks savepoints)."""
    cursor.execute("SET LOCAL myapp.current_user_id = %s;", (user_id,))
    cursor.execute("SET LOCAL myapp.current_org_id = %s;", (org_id,))


@celery_app.task(name="migrate_kb_to_memory", bind=True, max_retries=0)
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
                        _set_rls_for_row(cursor, org_id, user_id)
                        cursor.execute(
                            """INSERT INTO artifacts
                                   (org_id, user_id, title, content, category, description,
                                    last_edited_by, updated_at)
                               VALUES (%s, %s, 'Org Context', %s, 'context',
                                       'User-provided org context (migrated from knowledge base)', 'user', CURRENT_TIMESTAMP)
                               ON CONFLICT (org_id, category, title) DO NOTHING
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
                # Table is org-scoped (org_id PK only) — resolve a user for RLS
                cursor.execute(
                    "SELECT org_id, content FROM infrastructure_context WHERE content IS NOT NULL AND content != ''"
                )
                infra_rows = cursor.fetchall()

                for org_id, content in infra_rows:
                    user_id = _resolve_user_for_org(cursor, org_id)
                    if not user_id:
                        logger.warning("[Migration] No user found for org %s, skipping infra context", org_id)
                        stats["errors"] += 1
                        continue

                    cursor.execute("SAVEPOINT migrate_infra_row")
                    try:
                        _set_rls_for_row(cursor, org_id, user_id)
                        cursor.execute(
                            """INSERT INTO artifacts
                                   (org_id, user_id, title, content, category, description,
                                    last_edited_by, updated_at)
                               VALUES (%s, %s, 'Infrastructure Context', %s, 'infrastructure',
                                       'Auto-discovered infrastructure topology (migrated)', 'system', CURRENT_TIMESTAMP)
                               ON CONFLICT (org_id, category, title) DO NOTHING
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
                       WHERE status IN ('processed', 'ready')
                         AND storage_path IS NOT NULL"""
                )
                doc_rows = cursor.fetchall()

                for org_id, user_id, filename, storage_path in doc_rows:
                    cursor.execute("SAVEPOINT migrate_doc_row")
                    try:
                        storage = get_storage_manager()
                        raw_bytes = storage.download_bytes(_normalize_storage_path(storage_path))

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

                        _set_rls_for_row(cursor, org_id, user_id)

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
                                   ON CONFLICT (org_id, category, title) DO NOTHING
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
