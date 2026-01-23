"""
API cost tracking utility functions for Aurora
Tracks LLM API usage costs for users.
"""

import logging

from utils.db.connection_pool import db_pool

# Configure logging
logger = logging.getLogger(__name__)


def get_api_cost(user_id: str) -> float:
    """
    Get the total API usage cost for a user.
    
    Args:
        user_id: The user ID to get costs for

    Returns:
        float: Total API cost with surcharge
    """
    try:
        with db_pool.get_user_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SET myapp.current_user_id = %s;", (user_id,))

            cursor.execute(
                """
                SELECT COALESCE(SUM(total_cost_with_surcharge), 0) as total_cost
                FROM llm_usage_tracking
                WHERE user_id = %s
            """, (user_id,))
            
            result = cursor.fetchone()
            if result and result[0] is not None:
                api_cost = float(result[0])
            else:
                api_cost = 0.0

            logger.debug(f"API cost for user {user_id}: ${api_cost:.2f}")
            return api_cost

    except Exception as e:
        logger.error(
            f"Error getting API cost for user {user_id}: {type(e).__name__}: {e}\n{traceback.format_exc()}"
        )
        return 0.0

