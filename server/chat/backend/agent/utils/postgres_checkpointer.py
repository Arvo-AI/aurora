# Postgres checkpointer for LangGraph multi-agent workflows.

import asyncio
import logging
import os

from .safe_memory_saver import SafeMemorySaver

logger = logging.getLogger(__name__)

_saver = None
_pool = None
_lock = asyncio.Lock()


def _build_conn_string() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.getenv("POSTGRES_PASSWORD", "")
    host = os.environ["POSTGRES_HOST"]
    port = os.environ["POSTGRES_PORT"]
    dbname = os.environ["POSTGRES_DB"]
    sslmode = os.getenv("POSTGRES_SSLMODE", "")

    auth = f"{user}:{password}" if password else user
    uri = f"postgresql://{auth}@{host}:{port}/{dbname}"
    if sslmode:
        uri = f"{uri}?sslmode={sslmode}"
    return uri


async def get_postgres_checkpointer():
    global _saver, _pool

    if _saver is not None:
        return _saver

    async with _lock:
        if _saver is not None:
            return _saver

        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
            from psycopg_pool import AsyncConnectionPool
        except ImportError:
            logger.exception("langgraph-checkpoint-postgres / psycopg not installed")
            raise

        conn_string = _build_conn_string()

        try:
            pool = AsyncConnectionPool(
                conninfo=conn_string,
                max_size=20,
                kwargs={"autocommit": True, "prepare_threshold": 0},
                open=False,
            )
            await pool.open()
            saver = AsyncPostgresSaver(conn=pool)
        except Exception:
            logger.exception("Failed to construct AsyncPostgresSaver")
            raise

        try:
            await saver.setup()
        except Exception:
            logger.exception("AsyncPostgresSaver.setup() failed (continuing — likely idempotent)")

        _pool = pool
        _saver = saver
        logger.info("AsyncPostgresSaver initialised")
        return _saver


async def close() -> None:
    global _saver, _pool
    if _pool is not None:
        try:
            await _pool.close()
        except Exception:
            logger.exception("Error closing AsyncPostgresSaver pool")
    _saver = None
    _pool = None


def get_checkpointer():
    """Sync entry point — always returns SafeMemorySaver.

    For the durable AsyncPostgresSaver, callers must `await get_postgres_checkpointer()`
    from an async context. This sync helper exists for legacy/single-agent code paths
    (Workflow.__init__) that can't await; trying to drive the async helper from a sync
    helper inside a running loop deadlocks (asyncio.run forbids nested loops, and
    loop.run_until_complete forbids running loops).
    """
    if os.getenv("ENABLE_POSTGRES_CHECKPOINTER", "").lower() in ("1", "true", "yes"):
        logger.info(
            "ENABLE_POSTGRES_CHECKPOINTER set, but get_checkpointer() is sync; "
            "single-agent paths use SafeMemorySaver. Multi-agent path uses the durable saver."
        )
    return SafeMemorySaver()
