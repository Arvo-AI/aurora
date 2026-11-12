"""Indexes for the monitor's chat audit, built without blocking chat writes.

``initialize_tables`` runs every DDL statement in one transaction, where a plain
``CREATE INDEX`` holds a lock that blocks writes to the table for the whole
build. On a large ``chat_sessions`` table the full-text index can take minutes,
so these indexes are built separately with ``CREATE INDEX CONCURRENTLY``, on
their own autocommit connection, after the init transaction has committed.

* Only one process builds at a time: a session advisory try-lock, so other
  replicas (and the API, chatbot and worker processes) skip instead of waiting.
* An interrupted concurrent build leaves an INVALID index that ``IF NOT
  EXISTS`` would keep forever, so INVALID leftovers are dropped and rebuilt.
* Nothing here can fail startup: errors are logged, and queries still work
  without the indexes, only slower.

This module has no imports from the application, so the init path can't fail
on an import error. ``SEARCH_DOC_SQL`` is the query-side expression of the
full-text index; ``services.monitor.chat_audit`` uses it, and a test keeps the
two identical (Postgres only uses the index when they match).
"""

import logging
import threading

import psycopg2

logger = logging.getLogger(__name__)

_ADVISORY_LOCK_KEY = 1729_0001

_SEARCH_EXPR = (
    "to_tsvector('simple'::regconfig, "
    "left((jsonb_path_query_array({col}, '$[*].text'::jsonpath))::text, 200000))"
)
SEARCH_DOC_SQL = _SEARCH_EXPR.format(col="cs.messages")

# (index name, table, column/expression list)
INDEXES = (
    ("idx_execution_steps_org_status_time", "execution_steps", "(org_id, status, started_at DESC)"),
    ("idx_execution_steps_root_session", "execution_steps", "(org_id, (split_part(session_id, '::', 1)))"),
    ("idx_chat_sessions_org_updated", "chat_sessions", "(org_id, updated_at DESC)"),
    ("idx_chat_sessions_message_fts", "chat_sessions",
     f"USING GIN (({_SEARCH_EXPR.format(col='messages')}))"),
)


def build_indexes(connect) -> None:
    """Build every missing index concurrently. ``connect()`` returns a new DB connection."""
    conn = None
    try:
        conn = connect()
        conn.autocommit = True  # CREATE INDEX CONCURRENTLY can't run in a transaction
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,))
            if not cur.fetchone()[0]:
                logger.info("[ChatAuditIndexes] another process is building them; skipping")
                return
            try:
                # Waits on old transactions are bounded; a build that times out is
                # left INVALID and retried on the next boot.
                cur.execute("SET lock_timeout = '30s'")
                for name, table, definition in INDEXES:
                    _build_one(cur, name, table, definition)
            finally:
                cur.execute("SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_KEY,))
    except Exception as exc:
        logger.warning("[ChatAuditIndexes] index build skipped: %s", exc)
    finally:
        if conn is not None:
            conn.close()


def _build_one(cur, name: str, table: str, definition: str) -> None:
    try:
        cur.execute(
            "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relname = %s",
            (name,),
        )
        row = cur.fetchone()
        if row and row[0]:
            return
        if row:  # INVALID leftover from an interrupted concurrent build
            logger.info("[ChatAuditIndexes] dropping invalid index %s", name)
            cur.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
        # Names, tables and definitions are module constants, never input.
        cur.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} {definition}")
        logger.info("[ChatAuditIndexes] built %s", name)
    except Exception as exc:
        logger.warning("[ChatAuditIndexes] could not build %s: %s", name, exc)


def build_indexes_in_background(db_params: dict) -> threading.Thread:
    """Start the build on a daemon thread so startup never waits on it."""
    thread = threading.Thread(
        target=build_indexes,
        args=(lambda: psycopg2.connect(**db_params),),
        name="chat-audit-indexes",
        daemon=True,
    )
    thread.start()
    return thread
