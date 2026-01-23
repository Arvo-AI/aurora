"""LLM usage tracking API routes."""
import logging
from flask import Blueprint, request, jsonify
from utils.auth.stateless_auth import get_user_id_from_request
from utils.web.cors_utils import create_cors_response
from utils.db.connection_pool import db_pool

# Configure logging
logger = logging.getLogger(__name__)

llm_usage_bp = Blueprint('llm_usage', __name__)

@llm_usage_bp.route('/api/llm-usage/models', methods=['GET', 'OPTIONS'])
def get_available_models():
    """Get list of models used by the user."""
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    user_id = get_user_id_from_request()
    if not user_id:
        logger.warning("Missing user_id in available models request")
        return jsonify({"error": "Missing user_id"}), 400
    
    try:
        with db_pool.get_user_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
            
            # Get unique LLM models used by the user
            cursor.execute("""
                SELECT 
                    model_name,
                    COUNT(*) as usage_count,
                    SUM(estimated_cost) as total_cost,
                    SUM(surcharge_amount) as total_surcharge,
                    SUM(total_cost_with_surcharge) as total_cost_with_surcharge,
                    MIN(timestamp) as first_used,
                    MAX(timestamp) as last_used
                FROM llm_usage_tracking
                WHERE user_id = %s
                GROUP BY model_name
                ORDER BY usage_count DESC
            """, (user_id,))
            
            models = cursor.fetchall()
            
            formatted_models = []
            for model in models:
                formatted_model = {
                    "model_name": model[0],
                    "usage_count": model[1],
                    "total_cost": float(model[2]) if model[2] else 0.0,
                    "total_surcharge": float(model[3]) if model[3] else 0.0,
                    "total_cost_with_surcharge": float(model[4]) if model[4] else 0.0,
                    "first_used": model[5].isoformat() if model[5] else None,
                    "last_used": model[6].isoformat() if model[6] else None
                }
                formatted_models.append(formatted_model)
        
        # Calculate totals
        total_api_cost = sum(model["total_cost_with_surcharge"] for model in formatted_models)
        
        result = {
            "models": formatted_models,
            "total_models": len(formatted_models),
            "billing_summary": {
                "total_api_cost": total_api_cost,
                "total_cost": total_api_cost,
                "currency": "USD"
            }
        }

        logger.info(f"Retrieved {len(formatted_models)} models for user {user_id}")
        return jsonify(result)
    
    except Exception as e:
        logger.error(f"Error retrieving available models: {e}")
        return jsonify({"error": "Failed to retrieve models"}), 500 