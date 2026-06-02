"""
Database adapter functions for backward compatibility.
These functions allow existing code to work with the new connection pool without immediate refactoring.
"""

import logging
import time
from utils.db.connection_pool import db_pool, DatabaseConnectionPool
from contextlib import contextmanager
import psycopg2

logger = logging.getLogger(__name__)

_POOL_RETRY_ATTEMPTS = 3
_POOL_RETRY_BASE_DELAY = 0.1

class PooledConnectionWrapper:
    """Wrapper that makes a pooled connection behave like a regular connection but returns to pool on close."""
    
    def __init__(self, connection, pool, is_admin=False):
        self._connection = connection
        self._pool = pool
        self._is_admin = is_admin
        self._closed = False
    
    def __getattr__(self, name):
        # Delegate all other methods to the real connection
        return getattr(self._connection, name)
    
    def close(self):
        """Override close to return connection to pool instead of actually closing."""
        if not self._closed:
            try:
                self._connection.rollback()
                with self._connection.cursor() as cur:  # No RLS needed — pool cleanup (RESET vars)
                    cur.execute(
                        "RESET myapp.current_user_id; RESET myapp.current_org_id;"
                    )
                self._connection.commit()
            except Exception as e:
                logger.warning("Failed to reset session vars on pool return: %s", e)
            try:
                self._pool.putconn(self._connection)
                self._closed = True
            except Exception as e:
                logger.error("Error returning connection to pool on close(): %s", e)
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

def _getconn_with_retry(pool, label="connection"):
    for attempt in range(_POOL_RETRY_ATTEMPTS):
        try:
            connection = pool.getconn()
            if connection:
                return connection
        except psycopg2.pool.PoolError:
            logger.debug("Pool exhausted on attempt %d, will retry", attempt + 1)
        if attempt < _POOL_RETRY_ATTEMPTS - 1:
            delay = _POOL_RETRY_BASE_DELAY * (2 ** attempt)
            logger.warning("Pool exhausted getting %s, retrying in %.2fs (attempt %d/%d)",
                           label, delay, attempt + 1, _POOL_RETRY_ATTEMPTS)
            time.sleep(delay)
    raise Exception("connection pool exhausted")


def connect_to_db_as_admin():
    """
    Backward compatible function that returns a connection from the admin pool.

    WARNING: This function is for backward compatibility only.
    New code should use db_pool.get_admin_connection() context manager instead.

    Returns:
        PooledConnectionWrapper: Database connection that automatically returns to pool on close()
    """
    pool = db_pool._get_pool()
    connection = _getconn_with_retry(pool, "admin")
    connection.autocommit = False
    DatabaseConnectionPool._set_rls_vars(connection)
    return PooledConnectionWrapper(connection, pool, is_admin=True)

def connect_to_db_as_user():
    """
    Backward compatible function that returns a connection from the user pool.

    WARNING: This function is for backward compatibility only.
    New code should use db_pool.get_user_connection() context manager instead.

    Returns:
        PooledConnectionWrapper: Database connection that automatically returns to pool on close()
    """
    pool = db_pool._get_pool()
    connection = _getconn_with_retry(pool, "user")
    connection.autocommit = False
    DatabaseConnectionPool._set_rls_vars(connection)
    return PooledConnectionWrapper(connection, pool, is_admin=False)

def return_connection_to_pool(connection, is_admin=False):
    """
    Return a connection back to the appropriate pool.
    This should be called when done with connections obtained via the backward compatibility functions.
    
    Args:
        connection: The database connection to return
        is_admin: Whether this is an admin connection
    """
    try:
        if hasattr(connection, 'close'):
            connection.close()  # This will use our wrapper's close method
        else:
            # Fallback for unwrapped connections - now uses unified pool
            pool = db_pool._get_pool()
            pool.putconn(connection)
            logger.debug("Returned connection to pool")
    except Exception as e:
        logger.error(f"Error returning connection to pool: {e}")

@contextmanager
def get_admin_connection_legacy():
    """
    Context manager for admin connections (legacy wrapper).
    Use this when migrating from the old pattern gradually.
    """
    connection = None
    try:
        connection = connect_to_db_as_admin()
        yield connection
    except Exception as e:
        if connection:
            try:
                connection.rollback()
            except Exception:
                pass
        raise
    finally:
        if connection:
            return_connection_to_pool(connection, is_admin=True)

@contextmanager
def get_user_connection_legacy():
    """
    Context manager for user connections (legacy wrapper).
    Use this when migrating from the old pattern gradually.
    """
    connection = None
    try:
        connection = connect_to_db_as_user()
        yield connection
    except Exception as e:
        if connection:
            try:
                connection.rollback()
            except Exception:
                pass
        raise
    finally:
        if connection:
            return_connection_to_pool(connection, is_admin=False) 