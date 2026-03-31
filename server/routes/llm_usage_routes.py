"""LLM usage tracking API routes."""
import logging
from flask import Blueprint, request, jsonify
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request
from utils.web.cors_utils import create_cors_response
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

llm_usage_bp = Blueprint('llm_usage', __name__)

@llm_usage_bp.route('/api/llm-usage/models', methods=['OPTIONS'])
def get_available_models_options():
    return create_cors_response()


@llm_usage_bp.route('/api/llm-usage/models', methods=['GET'])
@require_permission("llm_usage", "read")
def get_available_models(user_id):
    """Get list of models used across the org."""
    try:
        org_id = get_org_id_from_request()
        with db_pool.get_user_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
            if org_id:
                cursor.execute("SET myapp.current_org_id = %s;", (org_id,))
            
            # Query org-wide usage when org_id available, else fall back to user
            if org_id:
                cursor.execute("""
                    SELECT 
                        model_name,
                        COUNT(*) as usage_count,
                        SUM(estimated_cost) as total_cost,
                        SUM(input_tokens) as total_input_tokens,
                        SUM(output_tokens) as total_output_tokens,
                        SUM(total_tokens) as total_tokens,
                        MIN(timestamp) as first_used,
                        MAX(timestamp) as last_used
                    FROM llm_usage_tracking
                    WHERE org_id = %s
                    GROUP BY model_name
                    ORDER BY usage_count DESC
                """, (org_id,))
            else:
                cursor.execute("""
                    SELECT 
                        model_name,
                        COUNT(*) as usage_count,
                        SUM(estimated_cost) as total_cost,
                        SUM(input_tokens) as total_input_tokens,
                        SUM(output_tokens) as total_output_tokens,
                        SUM(total_tokens) as total_tokens,
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
                formatted_models.append({
                    "model_name": model[0],
                    "usage_count": model[1],
                    "total_cost": float(model[2]) if model[2] else 0.0,
                    "total_input_tokens": model[3] or 0,
                    "total_output_tokens": model[4] or 0,
                    "total_tokens": model[5] or 0,
                    "first_used": model[6].isoformat() if model[6] else None,
                    "last_used": model[7].isoformat() if model[7] else None,
                })

            org_total_cost = None
            if org_id:
                cursor.execute("""
                    SELECT COALESCE(SUM(estimated_cost), 0)
                    FROM llm_usage_tracking
                    WHERE org_id = %s
                """, (org_id,))
                row = cursor.fetchone()
                org_total_cost = float(row[0]) if row else 0.0
        
        total_api_cost = sum(m["total_cost"] for m in formatted_models)
        
        result = {
            "models": formatted_models,
            "total_models": len(formatted_models),
            "billing_summary": {
                "total_api_cost": total_api_cost,
                "total_cost": total_api_cost,
                "currency": "USD",
            },
        }
        if org_total_cost is not None:
            result["billing_summary"]["org_total_cost"] = org_total_cost

        logger.info(f"Retrieved {len(formatted_models)} models for user {user_id}")
        return jsonify(result)
    
    except Exception as e:
        logger.error(f"Error retrieving available models: {e}")
        return jsonify({"error": "Failed to retrieve models"}), 500


@llm_usage_bp.route('/api/llm-usage/session/<session_id>', methods=['OPTIONS'])
def get_session_usage_options(session_id):
    return create_cors_response()


@llm_usage_bp.route('/api/llm-usage/session/<session_id>', methods=['GET'])
@require_permission("llm_usage", "read")
def get_session_usage(user_id, session_id):
    """Get per-request token/cost breakdown for a specific session (org-visible)."""
    try:
        with db_pool.get_user_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SET myapp.current_user_id = %s;", (user_id,))

            cursor.execute("""
                SELECT
                    id, model_name, api_provider, request_type,
                    input_tokens, output_tokens, total_tokens,
                    estimated_cost, response_time_ms,
                    request_metadata, timestamp
                FROM llm_usage_tracking
                WHERE session_id = %s
                ORDER BY timestamp ASC
            """, (session_id,))

            rows = cursor.fetchall()

            requests = []
            totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost": 0.0, "request_count": 0}

            for row in rows:
                input_t = row[4] or 0
                output_t = row[5] or 0
                total_t = row[6] or 0
                cost = float(row[7]) if row[7] else 0.0

                requests.append({
                    "id": row[0],
                    "model_name": row[1],
                    "api_provider": row[2],
                    "request_type": row[3],
                    "input_tokens": input_t,
                    "output_tokens": output_t,
                    "total_tokens": total_t,
                    "estimated_cost": cost,
                    "response_time_ms": row[8],
                    "metadata": row[9],
                    "timestamp": row[10].isoformat() if row[10] else None,
                })

                totals["input_tokens"] += input_t
                totals["output_tokens"] += output_t
                totals["total_tokens"] += total_t
                totals["cost"] += cost
                totals["request_count"] += 1

        return jsonify({
            "session_id": session_id,
            "requests": requests,
            "totals": totals,
        })

    except Exception as e:
        logger.error(f"Error retrieving session usage for {session_id}: {e}")
        return jsonify({"error": "Failed to retrieve session usage"}), 500


@llm_usage_bp.route('/api/llm-usage/pricing', methods=['OPTIONS'])
def get_pricing_options():
    return create_cors_response()


@llm_usage_bp.route('/api/llm-usage/pricing', methods=['GET'])
@require_permission("llm_usage", "read")
def get_pricing(user_id):
    """Get current raw model pricing (per 1K tokens, no markup)."""
    try:
        from chat.backend.agent.utils.llm_usage_tracker import LLMUsageTracker
        import os

        pricing_info = LLMUsageTracker.get_pricing_info()
        provider_mode = os.getenv("LLM_PROVIDER_MODE", "direct")

        models = {}
        for model_id, prices in LLMUsageTracker.MODEL_PRICING.items():
            if model_id == "default":
                continue
            models[model_id] = {
                "input_per_1k": prices.get("input", 0),
                "output_per_1k": prices.get("output", 0),
                "input_per_1m": prices.get("input", 0) * 1000,
                "output_per_1m": prices.get("output", 0) * 1000,
            }

        return jsonify({
            "models": models,
            "provider_mode": provider_mode,
            "pricing_info": pricing_info,
            "currency": "USD",
        })

    except Exception as e:
        logger.error(f"Error retrieving pricing: {e}")
        return jsonify({"error": "Failed to retrieve pricing"}), 500
