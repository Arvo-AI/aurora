import psycopg2
import psycopg2.pool
import logging
import os
import time
import threading
from dotenv import load_dotenv
from contextlib import contextmanager
from typing import Optional
from flask import has_request_context, request

load_dotenv()

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# How long a caller waits for a pooled connection before giving up (seconds).
# DB_POOL_WAIT_TIMEOUT tunes it per deployment; the readiness probe passes its
# own, shorter value to get_connection().
_POOL_WAIT_TIMEOUT = 5.0


def _env_number(name, default, cast):
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        logger.warning("%s=%r is not a number; using %s", name, raw, default)
        return default


def _pool_wait_timeout() -> float:
    return _env_number('DB_POOL_WAIT_TIMEOUT', _POOL_WAIT_TIMEOUT, float)


def _connect_kwargs_from_env() -> dict:
    """psycopg2 connection parameters from the POSTGRES_* environment.

    ``connect_timeout`` (DB_CONNECT_TIMEOUT, default 10s) makes an unreachable
    Postgres fail fast instead of hanging a request thread, and the readiness
    probe behind it, on TCP connect. The TCP keepalives let the kernel notice
    a dead peer, so a connection stuck on a hung Postgres or a dropped network
    path errors out and leaves the pool instead of being held forever.
    """
    params = {
        'dbname': os.getenv('POSTGRES_DB'),
        'user': os.getenv('POSTGRES_USER'),
        'password': os.getenv('POSTGRES_PASSWORD'),
        'host': os.getenv('POSTGRES_HOST'),
        'port': int(os.getenv('POSTGRES_PORT')),
        'connect_timeout': _env_number('DB_CONNECT_TIMEOUT', 10, int),
        'keepalives': 1,
        'keepalives_idle': 30,
        'keepalives_interval': 10,
        'keepalives_count': 3,
    }
    pg_sslmode = os.getenv('POSTGRES_SSLMODE', 'prefer')
    if pg_sslmode:
        params['sslmode'] = pg_sslmode
        pg_sslrootcert = os.getenv('POSTGRES_SSLROOTCERT')
        if pg_sslrootcert:
            params['sslrootcert'] = pg_sslrootcert
    return params


class DatabaseConnectionPool:
    """Centralized database connection pool manager."""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super(DatabaseConnectionPool, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, '_initialized'):
            return

        # Unified database configuration using POSTGRES_* env vars
        self.db_params = _connect_kwargs_from_env()

        self.min_connections = int(os.getenv('DB_POOL_MIN', '2'))
        self.max_connections = int(os.getenv('DB_POOL_MAX', '20'))

        # Single connection pool
        self._pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None

        # Track which PID created the pool so we can detect post-fork reuse
        self._pool_pid: Optional[int] = None

        # Condition variable for waiting when pool is exhausted
        self._pool_available = threading.Condition(threading.Lock())

        # Initialize pool on first access
        self._pool_lock = threading.Lock()
        self._initialized = True

        logger.info("DatabaseConnectionPool initialized")

    def _get_pool(self) -> psycopg2.pool.ThreadedConnectionPool:
        """Get or create the connection pool.

        Detects process forks (e.g. Gunicorn with --preload) and recreates
        the pool in child workers. psycopg2 connections are not fork-safe.
        """
        current_pid = os.getpid()

        if self._pool is not None and self._pool_pid != current_pid:
            logger.warning(
                "Connection pool was created in PID %s but current PID is %s "
                "(post-fork). Discarding inherited pool and creating a new one.",
                self._pool_pid, current_pid,
            )
            with self._pool_lock:
                self._pool = None
                self._pool_pid = None

        if self._pool is None:
            with self._pool_lock:
                if self._pool is None:
                    try:
                        self._pool = psycopg2.pool.ThreadedConnectionPool(
                            self.min_connections,
                            self.max_connections,
                            **self.db_params
                        )
                        self._pool_pid = current_pid
                        logger.info(
                            "Connection pool created (PID %s): %s-%s connections",
                            current_pid, self.min_connections, self.max_connections,
                        )
                    except Exception as e:
                        logger.error(f"Failed to create connection pool: {e}")
                        raise
        return self._pool

    def _getconn_with_retry(self, pool, wait_timeout: Optional[float] = None):
        """Get a connection, waiting up to ``wait_timeout`` seconds if exhausted.

        psycopg2's ThreadedConnectionPool raises PoolError immediately when
        all connections are checked out. This wrapper retries with backoff so
        that short-lived queries (sub-agents, tool callbacks) don't fail just
        because they collided at the same instant.

        ``wait_timeout`` defaults to ``DB_POOL_WAIT_TIMEOUT`` (5s). Callers that
        must not block a request thread for long (health probes) pass a small
        value; ``0`` means "fail immediately if the pool is empty".
        """
        if wait_timeout is None:
            wait_timeout = _pool_wait_timeout()
        deadline = time.monotonic() + wait_timeout
        attempt = 0
        while True:
            try:
                return pool.getconn()
            except psycopg2.pool.PoolError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                attempt += 1
                if attempt == 1:
                    logger.warning(
                        "Connection pool exhausted — waiting up to %.1fs for a free connection",
                        remaining,
                    )
                with self._pool_available:
                    self._pool_available.wait(timeout=min(0.1, remaining))

    def _putconn_notify(self, pool, connection):
        """Return a connection to the pool and notify waiters."""
        pool.putconn(connection)
        with self._pool_available:
            self._pool_available.notify()

    @contextmanager
    def get_connection(self, wait_timeout: Optional[float] = None):
        """Get a connection from the pool with automatic cleanup.

        Automatically sets RLS session variables (myapp.current_user_id,
        myapp.current_org_id) from the Flask request context when available.
        This ensures all queries on RLS-protected tables work correctly
        without callers needing to SET them manually.

        ``wait_timeout`` bounds how long to wait for a free pooled connection
        (default ``DB_POOL_WAIT_TIMEOUT``, 5s); a ``psycopg2.pool.PoolError``
        is raised when it expires.
        """
        pool = self._get_pool()
        connection = None
        try:
            connection = self._getconn_with_retry(pool, wait_timeout)
            if connection:
                connection.autocommit = False
                self._set_rls_vars(connection)
                logger.debug("Retrieved connection from pool")
                yield connection
            else:
                raise Exception("Failed to get connection from pool")
        except Exception as e:
            if connection:
                try:
                    connection.rollback()
                except Exception:
                    pass  # rollback is best-effort during error handling
            logger.error(f"Error with connection: {e}")
            raise
        finally:
            if connection:
                try:
                    connection.rollback()
                    with connection.cursor() as cur:  # No RLS needed — pool cleanup (RESET vars)
                        cur.execute(
                            "RESET myapp.current_user_id; RESET myapp.current_org_id;"
                        )
                    connection.commit()
                except Exception as e:
                    logger.warning("Failed to reset session vars on pool return: %s", e)
                try:
                    self._putconn_notify(pool, connection)
                except Exception as e:
                    logger.error("Error returning connection to pool: %s", e)

    @staticmethod
    def _set_rls_vars(connection):
        """Set RLS session variables from Flask request context if available."""
        try:
            if not has_request_context():
                return
            from flask import g
            user_id = request.headers.get('X-User-ID')
            org_id = request.headers.get('X-Org-ID') or getattr(g, '_org_id_resolved', None) or None
            if user_id or org_id:
                with connection.cursor() as cur:  # No RLS needed — auto-setting RLS vars for Flask request
                    if user_id:
                        cur.execute("SET myapp.current_user_id = %s", (user_id,))
                    if org_id:
                        cur.execute("SET myapp.current_org_id = %s", (org_id,))
            elif not request.path.startswith("/health"):
                logger.warning(
                    "No user_id or org_id available in request context for %s %s ",
                    request.method, request.path,
                )
        except Exception as exc:
            logger.debug("_set_rls_vars failed, continuing without RLS context: %s", exc)

    # Backward compatibility aliases
    def get_user_connection(self):
        """Alias for get_connection() - kept for backward compatibility."""
        return self.get_connection()

    def get_admin_connection(self):
        """Alias for get_connection() - kept for backward compatibility."""
        return self.get_connection()

    def get_pool_status(self) -> dict:
        """Get status information about the connection pool."""
        status = {'pool': None}

        if self._pool:
            status['pool'] = {
                'min_connections': self.min_connections,
                'max_connections': self.max_connections,
                # psycopg2 keeps checked-out connections in the private ``_used``
                # dict; exposing its size lets /health report pool pressure.
                'in_use': len(getattr(self._pool, '_used', {}) or {}),
                'closed': self._pool.closed
            }

        return status

    def test_connection_availability(self) -> dict:
        """Test if we can get a connection from the pool."""
        result = {
            'pool_available': False,
            'pool_error': None
        }

        try:
            with self.get_connection():
                result['pool_available'] = True
        except Exception as e:
            result['pool_error'] = str(e)

        return result

    def close_pools(self):
        """Close the connection pool."""
        with self._pool_lock:
            if self._pool and not self._pool.closed:
                self._pool.closeall()
                logger.info("Connection pool closed")

# Global instance
db_pool = DatabaseConnectionPool()
