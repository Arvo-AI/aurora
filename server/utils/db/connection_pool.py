import psycopg2
import psycopg2.pool
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
            'port': int(os.getenv('POSTGRES_PORT'))
        }

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
    
    @contextmanager
    def get_connection(self):
        """Get a connection from the pool with automatic cleanup."""
        pool = self._get_pool()
        connection = None
        try:
            connection = pool.getconn()
            if connection:
                connection.autocommit = False
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
                    pool.putconn(connection)
                    logger.debug("Returned connection to pool")
                except Exception as e:
                    logger.error(f"Error returning connection to pool: {e}")

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