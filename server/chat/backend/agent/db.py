import psycopg2
import os
from dotenv import load_dotenv
import logging
from utils.db.connection_pool import db_pool

load_dotenv()
logging.basicConfig(level=logging.DEBUG)

class PostgreSQLClient:
    def __init__(self):
        """
        Initialize the client to use connection pooling for better concurrent user support.
        This replaces the previous connection-per-query approach with proper pooling.
        """
        self.current_user_id = "unknown_user"

    def set_user_context(self, user_id: str) -> None:
        """
        Set user context - this will be applied per query.
        """
        self.current_user_id = user_id if user_id else "unknown_user"
        logging.debug(f"Set user context to: {self.current_user_id}")

    def execute_query(self, query: str, params: tuple = None) -> list:
        """
        Execute a query using the connection pool.
        This provides better performance and resource management for concurrent users.
        
        Args:
            query: SQL query to execute
            params: Query parameters
            
        Returns:
            list: Query results as list of dictionaries
        """
        try:
            # Use connection pool instead of creating new connections
            with db_pool.get_user_connection() as connection:
                with connection.cursor() as cursor:
                    # Set user context for RLS
                    cursor.execute("SET myapp.current_user_id = %s;", (self.current_user_id,))
                    
                    # Execute the main query
                    if params:
                        cursor.execute(query, params)
                    else:
                        cursor.execute(query)
                    
                    # For SELECT queries, return results as list of dictionaries
                    if query.strip().upper().startswith("SELECT"):
                        columns = [desc[0] for desc in cursor.description]
                        results = [dict(zip(columns, row)) for row in cursor.fetchall()]
                    else:
                        results = []
                
                connection.commit()
                return results
                
        except Exception as e:
            logging.error(f"Query execution failed: {e}")
            raise

    def get_schema(self) -> str:
        """
        Get table names from the database.
        """
        query = """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public';
        """
        results = self.execute_query(query)
        return str([row['table_name'] for row in results])

    def close(self):
        """Close method for backward compatibility - connection pool handles cleanup."""
        logging.debug("PostgreSQLClient.close() called - connection pool handles cleanup automatically")
        pass

if __name__ == "__main__":
    db = PostgreSQLClient()
    print("Schema info:", db.get_schema())
