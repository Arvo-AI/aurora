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

# How long to wait for a connection before giving up (seconds)
_POOL_WAIT_TIMEOUT = 5.0


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
        self.db_params = {
            'dbname': os.getenv('POSTGRES_DB'),
            'user': os.getenv('POSTGRES_USER'),
            'password': os.getenv('POSTGRES_PASSWORD'),
            'host': os.getenv('POSTGRES_HOST'),
            'port': int(os.getenv('POSTGRES_PORT'))
        }
        pg_sslmode = os.getenv('POSTGRES_SSLMODE', 'prefer')
        if pg_sslmode:
            self.db_params['sslmode'] = pg_sslmode
            pg_sslrootcert = os.getenv('POSTGRES_SSLROOTCERT')
            if pg_sslrootcert:
                self.db_params['sslrootcert'] = pg_sslrootcert

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

    def _getconn_with_retry(self, pool):
        """Get a connection, waiting up to _POOL_WAIT_TIMEOUT if exhausted.

        psycopg2's ThreadedConnectionPool raises PoolError immediately when
        all connections are checked out. This wrapper retries with backoff so
        that short-lived queries (sub-agents, tool callbacks) don't fail just
        because they collided at the same instant.
        """
        deadline = time.monotonic() + _POOL_WAIT_TIMEOUT
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
    def get_connection(self):
        """Get a connection from the pool with automatic cleanup.

        Downgrades to the aurora_app role (NOSUPERUSER NOBYPASSRLS) so that
        PostgreSQL RLS policies are enforced. Also sets RLS session variables
        (myapp.current_user_id, myapp.current_org_id) from the Flask request
        context when available.
        """
        pool = self._get_pool()
        connection = None
        try:
            connection = self._getconn_with_retry(pool)
            if connection:
                connection.autocommit = False
                self._downgrade_role(connection)
                self._set_rls_vars(connection)
                logger.debug("Retrieved connection from pool")
                yield connection
            else:
                raise Exception("Failed to get connection from pool")
        except Exception as e:
            if connection:
                try:
                    connection.rollback()
                except Exception as rollback_exc:
                    logger.debug("Rollback failed in connection error handler: %s", rollback_exc)
            logger.error(f"Error with connection: {e}")
            raise
        finally:
            if connection:
                reset_failed = False
                try:
                    connection.rollback()
                    with connection.cursor() as cur:
                        cur.execute(
                            "RESET ROLE; "
                            "RESET myapp.current_user_id; "
                            "RESET myapp.current_org_id; "
                            "RESET myapp.mcp_token_resolve; "
                            "RESET myapp.kubectl_token_resolve;"
                        )
                    connection.commit()
                except Exception as e:
                    logger.warning("Failed to reset session vars on pool return: %s", e)
                    reset_failed = True
                    try:
                        pool.putconn(connection, close=True)
                    except Exception as close_exc:
                        logger.debug("Failed to close broken connection during cleanup: %s", close_exc)
                    with self._pool_available:
                        self._pool_available.notify()
                if not reset_failed:
                    try:
                        self._putconn_notify(pool, connection)
                    except Exception as e:
                        logger.error("Error returning connection to pool: %s", e)

    @contextmanager
    def get_admin_connection(self):
        """Get a connection that retains superuser privileges (bypasses RLS).

        Use ONLY for: DDL/migrations, cross-org background tasks that
        explicitly call set_rls_context() per-org, and bootstrap queries
        (e.g. auth registration before org context exists).
        """
        pool = self._get_pool()
        connection = None
        try:
            connection = self._getconn_with_retry(pool)
            if connection:
                connection.autocommit = False
                logger.debug("Retrieved admin connection from pool (no role downgrade)")
                yield connection
            else:
                raise Exception("Failed to get connection from pool")
        except Exception as e:
            if connection:
                try:
                    connection.rollback()
                except Exception as rollback_exc:
                    logger.debug("Rollback failed in admin connection error handler: %s", rollback_exc)
            logger.error(f"Error with admin connection: {e}")
            raise
        finally:
            if connection:
                reset_failed = False
                try:
                    connection.rollback()
                    with connection.cursor() as cur:
                        cur.execute(
                            "RESET ROLE; "
                            "RESET myapp.current_user_id; "
                            "RESET myapp.current_org_id; "
                            "RESET myapp.mcp_token_resolve; "
                            "RESET myapp.kubectl_token_resolve;"
                        )
                    connection.commit()
                except Exception as e:
                    logger.warning("Failed to reset session vars on pool return: %s", e)
                    reset_failed = True
                    try:
                        pool.putconn(connection, close=True)
                    except Exception as close_exc:
                        logger.debug("Failed to close broken admin connection during cleanup: %s", close_exc)
                    with self._pool_available:
                        self._pool_available.notify()
                if not reset_failed:
                    try:
                        self._putconn_notify(pool, connection)
                    except Exception as e:
                        logger.error("Error returning connection to pool: %s", e)

    @staticmethod
    def _downgrade_role(connection):
        """Downgrade session to aurora_app so RLS is enforced.

        Raises RuntimeError if the role switch fails — connections must not
        be used without RLS enforcement (fail closed).
        """
        try:
            with connection.cursor() as cur:
                cur.execute("SET ROLE aurora_app;")
        except Exception as exc:
            logger.error("SET ROLE aurora_app failed; refusing to run without RLS enforcement: %s", exc)
            try:
                connection.rollback()
            except Exception as rollback_exc:
                logger.debug("Rollback after failed role downgrade also failed: %s", rollback_exc)
            raise RuntimeError("Failed to downgrade database role for RLS enforcement") from exc

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
                with connection.cursor() as cur:
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

    # Backward compatibility alias
    def get_user_connection(self):
        """Alias for get_connection() - kept for backward compatibility."""
        return self.get_connection()

    def get_pool_status(self) -> dict:
        """Get status information about the connection pool."""
        status = {'pool': None}

        if self._pool:
            status['pool'] = {
                'min_connections': self.min_connections,
                'max_connections': self.max_connections,
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
