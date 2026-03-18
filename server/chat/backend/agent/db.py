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
        self.current_org_id = None

    def set_user_context(self, user_id: str, org_id: str = None) -> None:
        """
        Set user and org context - applied per query for RLS enforcement.
        If org_id is not provided, it will be resolved from the database.
        """
        self.current_user_id = user_id if user_id else "unknown_user"
        if org_id:
            self.current_org_id = org_id
        elif user_id and user_id != "unknown_user":
            try:
                from utils.auth.stateless_auth import get_org_id_for_user
                self.current_org_id = get_org_id_for_user(user_id)
            except Exception:
                logging.warning(f"Could not resolve org_id for user {user_id}")
                self.current_org_id = None
        logging.debug(f"Set user context to: user={self.current_user_id}, org={self.current_org_id}")

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
                    # Set user + org context for RLS
                    cursor.execute("SET myapp.current_user_id = %s;", (self.current_user_id,))
                    if self.current_org_id:
                        cursor.execute("SET myapp.current_org_id = %s;", (self.current_org_id,))
                    
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
