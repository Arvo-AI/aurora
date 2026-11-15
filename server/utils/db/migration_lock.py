"""Startup migration lock: a bounded wait instead of an indefinite one."""

import logging
import math
import os
import time

logger = logging.getLogger(__name__)

MIGRATION_LOCK_KEY = 42
# Stays under the Helm startup probe window (~300s) so the pod reports a clear error
_DEFAULT_WAIT_SECONDS = 240.0
_POLL_SECONDS = 1.0
_LOG_EVERY_SECONDS = 30.0


class MigrationLockTimeout(RuntimeError):
    """The migration lock was still held by another session after the wait."""


def _wait_seconds() -> float:
    raw = os.getenv("DB_MIGRATION_LOCK_TIMEOUT")
    if not raw:
        return _DEFAULT_WAIT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logger.warning("DB_MIGRATION_LOCK_TIMEOUT=%r is not a number; using default", raw)
        return _DEFAULT_WAIT_SECONDS
    # nan/inf never reach the deadline and <= 0 fails at once; both defeat the bound
    if not math.isfinite(value) or value <= 0:
        logger.warning("DB_MIGRATION_LOCK_TIMEOUT=%r is out of range; using default", raw)
        return _DEFAULT_WAIT_SECONDS
    return value


def _raw_connection(conn):
    # PooledConnectionWrapper stores psycopg2 on _connection. Assigning
    # attributes on the wrapper never reaches it, and MagicMock invents
    # attributes, so only a real instance dict entry counts.
    inner = getattr(conn, "__dict__", {}).get("_connection")
    return inner if inner is not None else conn


def set_connection_autocommit(conn, enabled: bool) -> None:
    """Set autocommit on the psycopg2 connection, not a pool wrapper."""
    raw = _raw_connection(conn)
    if bool(raw.autocommit) == enabled:
        return
    # psycopg2 refuses to enable autocommit inside an open transaction
    if enabled:
        raw.rollback()
    raw.autocommit = enabled


def acquire_migration_lock(conn, key: int = MIGRATION_LOCK_KEY, wait_seconds=None) -> None:
    """Take the session-level advisory lock on an autocommit connection.

    Polls pg_try_advisory_lock so a holder that never releases (e.g. an orphaned
    session from a killed pod) fails this pod loudly rather than hanging it.
    """
    if wait_seconds is None:
        wait_seconds = _wait_seconds()

    # Sleeping inside an open transaction is idle-in-transaction for the whole
    # wait. `conn.autocommit = True` on the pool wrapper does not do this.
    set_connection_autocommit(conn, True)

    with conn.cursor() as cur:
        # Server-side keepalives let Postgres reap this session if the pod dies
        # without closing the socket, which would otherwise pin the lock forever.
        cur.execute("SET tcp_keepalives_idle = 30")
        cur.execute("SET tcp_keepalives_interval = 10")
        cur.execute("SET tcp_keepalives_count = 3")

        deadline = time.monotonic() + wait_seconds
        next_log = time.monotonic() + _LOG_EVERY_SECONDS
        while True:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
            if cur.fetchone()[0]:
                return

            now = time.monotonic()
            # Another pod is migrating; give up so the failure is visible
            if now >= deadline:
                raise MigrationLockTimeout(
                    f"Could not acquire migration advisory lock {key} within "
                    f"{wait_seconds:.0f}s; another session is holding it"
                )
            # Periodic progress so a waiting pod isn't silent
            if now >= next_log:
                logger.warning("Still waiting for migration lock %s", key)
                next_log = now + _LOG_EVERY_SECONDS
            time.sleep(_POLL_SECONDS)


def reset_migration_session(conn) -> None:
    """Clear the keepalive overrides before a pooled connection is reused."""
    try:
        set_connection_autocommit(conn, True)
        with _raw_connection(conn).cursor() as cur:
            cur.execute("RESET tcp_keepalives_idle")
            cur.execute("RESET tcp_keepalives_interval")
            cur.execute("RESET tcp_keepalives_count")
    except Exception:
        logger.warning("Could not reset migration-lock session settings")
