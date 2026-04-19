import psycopg2
import psycopg2.pool
import logging
import os
import threading
from dotenv import load_dotenv
from contextlib import contextmanager
from typing import Optional
from flask import has_request_context, request

load_dotenv()

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

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

        # Connection pool configuration
        self.min_connections = 1
        self.max_connections = 50

        # Single connection pool
        self._pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None

        # Track which PID created the pool so we can detect post-fork reuse
        self._pool_pid: Optional[int] = None

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
    
    @contextmanager
    def get_connection(self):
        """Get a connection from the pool with automatic cleanup.
        
        Automatically sets RLS session variables (myapp.current_user_id,
        myapp.current_org_id) from the Flask request context when available.
        This ensures all queries on RLS-protected tables work correctly
        without callers needing to SET them manually.
        """
        pool = self._get_pool()
        connection = None
        try:
            connection = pool.getconn()
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
                except:
                    pass
            logger.error(f"Error with connection: {e}")
            raise
        finally:
            if connection:
                try:
                    connection.rollback()
                    with connection.cursor() as cur:
                        cur.execute(
                            "RESET myapp.current_user_id; RESET myapp.current_org_id;"
                        )
                    connection.commit()
                except Exception as e:
                    logger.warning("Failed to reset session vars on pool return: %s", e)
                try:
                    pool.putconn(connection)
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