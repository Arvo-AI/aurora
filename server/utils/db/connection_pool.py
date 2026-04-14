import psycopg2
import psycopg2.pool
import psycopg2.extensions
import logging
import os
import threading
from dotenv import load_dotenv
from contextlib import contextmanager
from typing import Optional

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
            'port': int(os.getenv('POSTGRES_PORT')),
            'keepalives': 1,
            'keepalives_idle': 30,
            'keepalives_interval': 10,
            'keepalives_count': 5,
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

        # Initialize pool on first access
        self._pool_lock = threading.Lock()
        self._initialized = True

        logger.info("DatabaseConnectionPool initialized")
    
    def _get_pool(self) -> psycopg2.pool.ThreadedConnectionPool:
        """Get or create the connection pool."""
        if self._pool is None:
            with self._pool_lock:
                if self._pool is None:
                    try:
                        self._pool = psycopg2.pool.ThreadedConnectionPool(
                            self.min_connections,
                            self.max_connections,
                            **self.db_params
                        )
                        logger.info(f"Connection pool created: {self.min_connections}-{self.max_connections} connections")
                    except Exception as e:
                        logger.error(f"Failed to create connection pool: {e}")
                        raise
        return self._pool

    def _validate_connection(self, connection) -> bool:
        """Check if a pooled connection is still usable."""
        try:
            if connection.closed:
                return False
            status = connection.info.transaction_status
            if status == psycopg2.extensions.TRANSACTION_STATUS_UNKNOWN:
                return False
            old_autocommit = connection.autocommit
            connection.autocommit = True
            with connection.cursor() as cur:
                cur.execute("SELECT 1")
            connection.autocommit = old_autocommit
            return True
        except Exception:
            return False
    
    @contextmanager
    def get_connection(self, org_id: Optional[str] = None):
        """Get a connection from the pool with automatic cleanup.

        Args:
            org_id: If provided, ``SET myapp.current_org_id`` is executed on
                    the connection so that RLS policies see the caller's org.
        """
        pool = self._get_pool()
        connection = None
        try:
            connection = pool.getconn()
            if not connection or not self._validate_connection(connection):
                if connection:
                    try:
                        pool.putconn(connection, close=True)
                    except Exception:
                        pass
                connection = pool.getconn()
                if not connection:
                    raise Exception("Failed to get connection from pool")
            connection.autocommit = False
            if org_id:
                with connection.cursor() as cur:
                    cur.execute("SET myapp.current_org_id = %s", (org_id,))
            logger.debug("Retrieved connection from pool")
            yield connection
        except Exception as e:
            if connection:
                try:
                    connection.rollback()
                except Exception:
                    pass
            logger.error(f"Error with connection: {e}")
            raise
        finally:
            if connection:
                try:
                    if org_id:
                        with connection.cursor() as cur:
                            cur.execute("RESET myapp.current_org_id")
                        connection.commit()
                    pool.putconn(connection)
                    logger.debug("Returned connection to pool")
                except Exception as e:
                    logger.error(f"Error returning connection to pool: {e}")

    # Backward compatibility aliases
    def get_user_connection(self):
        """Alias for get_connection() - kept for backward compatibility."""
        return self.get_connection()

    def get_admin_connection(self, org_id: Optional[str] = None):
        """Get a connection with optional RLS org context."""
        return self.get_connection(org_id=org_id)
    
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