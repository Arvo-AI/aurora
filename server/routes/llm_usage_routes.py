"""LLM usage tracking API routes."""
import logging
from flask import Blueprint, request, jsonify
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request, set_rls_context
from utils.web.cors_utils import create_cors_response
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

llm_usage_bp = Blueprint('llm_usage', __name__)
_LOG_PREFIX = "[LLMUsage]"

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
            set_rls_context(cursor, conn, user_id, log_prefix=_LOG_PREFIX)
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


@llm_usage_bp.route('/api/llm-usage/cost-over-time', methods=['OPTIONS'])
def get_cost_over_time_options():
    return create_cors_response()


@llm_usage_bp.route('/api/llm-usage/summary', methods=['OPTIONS'])
def get_usage_summary_options():
    return create_cors_response()


@llm_usage_bp.route('/api/llm-usage/cost-over-time', methods=['GET'])
@require_permission("llm_usage", "read")
def get_cost_over_time(user_id):
    """Cost/token aggregates over time, optionally grouped by model or provider.
    
    Granularity adapts to period: <=7d uses hourly, <=90d uses daily, >90d uses weekly.
    Override with ?granularity=hour|day|week.
    """
    org_id = get_org_id_from_request()
    period = request.args.get("period", "30d")
    group_by = request.args.get("group_by", "model")
    granularity = request.args.get("granularity")

    interval_map = {"7d": "7 days", "30d": "30 days", "90d": "90 days", "180d": "180 days", "365d": "365 days"}
    pg_interval = interval_map.get(period, "30 days")

    if granularity in ("hour", "day", "week"):
        trunc = granularity
    elif period in ("7d",):
        trunc = "hour"
    elif period in ("180d", "365d"):
        trunc = "week"
    else:
        trunc = "day"

    group_col = "model_name" if group_by == "model" else "api_provider"

    try:
        with db_pool.get_user_connection() as conn:
            cursor = conn.cursor()
            set_rls_context(cursor, conn, user_id, log_prefix=_LOG_PREFIX)

            cursor.execute(f"""
                SELECT
                    date_trunc('{trunc}', timestamp) AS bucket,
                    {group_col} AS group_key,
                    SUM(estimated_cost) AS cost,
                    SUM(input_tokens) AS input_tokens,
                    SUM(output_tokens) AS output_tokens,
                    SUM(total_tokens) AS total_tokens,
                    COUNT(*) AS request_count
                FROM llm_usage_tracking
                WHERE {'org_id = %s' if org_id else 'user_id = %s'}
                  AND timestamp >= NOW() - %s::interval
                GROUP BY bucket, {group_col}
                ORDER BY bucket ASC, group_key ASC
            """, (org_id or user_id, pg_interval))

            rows = cursor.fetchall()

        data = []
        for row in rows:
            data.append({
                "date": row[0].isoformat() if row[0] else None,
                "group": row[1],
                "cost": float(row[2]) if row[2] else 0.0,
                "input_tokens": row[3] or 0,
                "output_tokens": row[4] or 0,
                "total_tokens": row[5] or 0,
                "request_count": row[6] or 0,
            })

        logger.info(
            "Cost-over-time fetched: %d points, group_by=%s, period=%s, granularity=%s",
            len(data), group_by, period, trunc,
        )
        return jsonify({"data": data, "group_by": group_by, "period": period, "granularity": trunc})
    except Exception as e:
        logger.error(f"Error fetching cost-over-time: {e}")
        return jsonify({"error": "Failed to fetch cost data"}), 500


@llm_usage_bp.route('/api/llm-usage/summary', methods=['GET'])
@require_permission("llm_usage", "read")
def get_usage_summary(user_id):
    """Aggregate usage summary for the period."""
    org_id = get_org_id_from_request()
    period = request.args.get("period", "30d")

    interval_map = {"7d": "7 days", "30d": "30 days", "90d": "90 days", "180d": "180 days", "365d": "365 days"}
    pg_interval = interval_map.get(period, "30 days")

    try:
        with db_pool.get_user_connection() as conn:
            cursor = conn.cursor()
            set_rls_context(cursor, conn, user_id, log_prefix=_LOG_PREFIX)

            cursor.execute(f"""
                SELECT
                    COALESCE(SUM(estimated_cost), 0) AS total_cost,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(input_tokens), 0) AS total_input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS total_output_tokens,
                    COUNT(*) AS total_requests,
                    COUNT(*) FILTER (WHERE error_message IS NOT NULL) AS error_count,
                    ROUND(AVG(response_time_ms) FILTER (WHERE response_time_ms IS NOT NULL)) AS avg_response_ms,
                    COUNT(DISTINCT model_name) AS models_used
                FROM llm_usage_tracking
                WHERE {'org_id = %s' if org_id else 'user_id = %s'}
                  AND timestamp >= NOW() - %s::interval
            """, (org_id or user_id, pg_interval))

            row = cursor.fetchone()

        total_requests = row[4] or 0
        error_count = row[5] or 0
        total_cost = float(row[0]) if row[0] else 0.0
        avg_response_ms = int(row[6]) if row[6] else None
        models_used = row[7] or 0
        error_rate = round(error_count / total_requests * 100, 1) if total_requests > 0 else 0

        logger.info(
            "Usage summary fetched: total_cost=%.4f, total_requests=%d, error_count=%d, error_rate=%s, avg_response_ms=%s, models_used=%d, period=%s",
            total_cost, total_requests, error_count, error_rate, avg_response_ms, models_used, period,
        )

        return jsonify({
            "total_cost": total_cost,
            "total_tokens": row[1] or 0,
            "total_input_tokens": row[2] or 0,
            "total_output_tokens": row[3] or 0,
            "total_requests": total_requests,
            "error_count": error_count,
            "error_rate": error_rate,
            "avg_response_ms": avg_response_ms,
            "models_used": models_used,
            "period": period,
        })
    except Exception as e:
        logger.error(f"Error fetching usage summary: {e}")
        return jsonify({"error": "Failed to fetch usage summary"}), 500


@llm_usage_bp.route('/api/llm-usage/session/<session_id>', methods=['GET'])
@require_permission("llm_usage", "read")
def get_session_usage(user_id, session_id):
    """Get per-request token/cost breakdown for a session."""
    try:
        org_id = get_org_id_from_request()
        with db_pool.get_user_connection() as conn:
            cursor = conn.cursor()
            set_rls_context(cursor, conn, user_id, log_prefix=_LOG_PREFIX)

            cursor.execute("""
                SELECT
                    model_name, input_tokens, output_tokens, total_tokens,
                    estimated_cost, response_time_ms, timestamp
                FROM llm_usage_tracking
                WHERE session_id = %s
                ORDER BY timestamp ASC
            """, (session_id,))

            rows = cursor.fetchall()

            requests = []
            total_input = 0
            total_output = 0
            total_cost = 0.0

            for row in rows:
                inp = row[1] or 0
                out = row[2] or 0
                cost = float(row[4]) if row[4] else 0.0
                requests.append({
                    "model": row[0],
                    "input_tokens": inp,
                    "output_tokens": out,
                    "total_tokens": row[3] or 0,
                    "estimated_cost": cost,
                    "response_time_ms": row[5] or 0,
                    "timestamp": row[6].isoformat() + "Z" if row[6] else None,
                })
                total_input += inp
                total_output += out
                total_cost += cost

        return jsonify({
            "requests": requests,
            "totals": {
                "total_input_tokens": total_input,
                "total_output_tokens": total_output,
                "total_cost": total_cost,
                "request_count": len(requests),
            },
        })

    except Exception as e:
        logger.error(f"Error retrieving session usage for {session_id}: {e}")
        return jsonify({"error": "Failed to retrieve session usage"}), 500
