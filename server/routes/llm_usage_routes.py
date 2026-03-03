"""LLM usage tracking API routes."""
import logging
from flask import Blueprint, request, jsonify
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request
from utils.web.cors_utils import create_cors_response
from utils.db.connection_pool import db_pool

# Configure logging
logger = logging.getLogger(__name__)

llm_usage_bp = Blueprint('llm_usage', __name__)

@llm_usage_bp.route('/api/llm-usage/models', methods=['OPTIONS'])
def get_available_models_options():
    return create_cors_response()


@llm_usage_bp.route('/api/llm-usage/models', methods=['GET'])
@require_permission("llm_usage", "read")
def get_available_models(user_id):
    """Get list of models used by the user, with org-level rollup."""
    try:
        org_id = get_org_id_from_request()
        with db_pool.get_user_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
            if org_id:
                cursor.execute("SET myapp.current_org_id = %s;", (org_id,))
            
            # Per-user billing breakdown
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

            # Org-level rollup (all members' usage)
            org_total_cost = None
            if org_id:
                cursor.execute("""
                    SELECT COALESCE(SUM(total_cost_with_surcharge), 0)
                    FROM llm_usage_tracking
                    WHERE org_id = %s
                """, (org_id,))
                row = cursor.fetchone()
                org_total_cost = float(row[0]) if row else 0.0
        
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
        if org_total_cost is not None:
            result["billing_summary"]["org_total_cost"] = org_total_cost

        logger.info(f"Retrieved {len(formatted_models)} models for user {user_id}")
        return jsonify(result)
    
    except Exception as e:
        logger.error(f"Error retrieving available models: {e}")
        return jsonify({"error": "Failed to retrieve models"}), 500 