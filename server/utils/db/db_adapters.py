"""
Database adapter functions for backward compatibility.
These functions allow existing code to work with the new connection pool without immediate refactoring.
"""

import logging
from utils.db.connection_pool import db_pool
from contextlib import contextmanager
import psycopg2

logger = logging.getLogger(__name__)

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
                self._pool.putconn(self._connection)
                self._closed = True
                logger.debug(f"Returned {'admin' if self._is_admin else 'user'} connection to pool via close()")
            except Exception as e:
                logger.error(f"Error returning connection to pool on close(): {e}")
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

def connect_to_db_as_admin():
    """
    Backward compatible function that returns a connection from the admin pool.
    
    WARNING: This function is for backward compatibility only.
    New code should use db_pool.get_admin_connection() context manager instead.
    
    Returns:
        PooledConnectionWrapper: Database connection that automatically returns to pool on close()
    """
    logger.debug("Using deprecated connect_to_db_as_admin(). Consider migrating to db_pool.get_admin_connection() context manager.")
    
    try:
        pool = db_pool._get_pool()
        connection = pool.getconn()
        if connection:
            connection.autocommit = False
            # Set default user context - admin connections typically don't need RLS context
            try:
                cursor = connection.cursor()
                cursor.execute("SET myapp.current_user_id = 'admin_user';")
                cursor.close()
            except Exception:
                # If setting fails, continue - admin connections may not need this
                pass
            logger.debug("Retrieved admin connection from pool (backward compatibility mode)")
            # Return wrapped connection that will return to pool on close()
            return PooledConnectionWrapper(connection, pool, is_admin=True)
        else:
            raise Exception("connection pool exhausted")
    except psycopg2.pool.PoolError:
        raise Exception("connection pool exhausted")
    except Exception as e:
        logger.error(f"Error getting admin connection: {e}")
        raise Exception("connection pool exhausted")

def connect_to_db_as_user():
    """
    Backward compatible function that returns a connection from the user pool.
    
    WARNING: This function is for backward compatibility only.
    New code should use db_pool.get_user_connection() context manager instead.
    
    Returns:
        PooledConnectionWrapper: Database connection that automatically returns to pool on close()
    """
    logger.debug("Using deprecated connect_to_db_as_user(). Consider migrating to db_pool.get_user_connection() context manager.")
    
    try:
        pool = db_pool._get_pool()
        connection = pool.getconn()
        if connection:
            connection.autocommit = False
            # Set default user context for RLS
            try:
                cursor = connection.cursor()
                cursor.execute("SET myapp.current_user_id = 'default_user';")
                cursor.close()
            except Exception:
                # If setting fails, continue - connection may still work for some queries
                pass
            logger.debug("Retrieved user connection from pool (backward compatibility mode)")
            # Return wrapped connection that will return to pool on close()
            return PooledConnectionWrapper(connection, pool, is_admin=False)
        else:
            raise Exception("connection pool exhausted")
    except psycopg2.pool.PoolError:
        raise Exception("connection pool exhausted")
    except Exception as e:
        logger.error(f"Error getting user connection: {e}")
        raise Exception("connection pool exhausted")

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
            except:
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
            except:
                pass
        raise
    finally:
        if connection:
            return_connection_to_pool(connection, is_admin=False) 