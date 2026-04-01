"""Waterfall routes -- per-incident execution timeline + aggregated tool performance stats."""
import logging
from flask import Blueprint, request, jsonify
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request, set_rls_context
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

waterfall_bp = Blueprint("monitor_waterfall", __name__)


@waterfall_bp.route("/api/monitor/incidents/<incident_id>/waterfall", methods=["GET"])
@require_permission("incidents", "read")
def incident_waterfall(user_id, incident_id):
    """Execution steps for a single incident, ordered by step_index."""
    org_id = get_org_id_from_request()

    query = """
        SELECT es.id,
               es.step_index,
               es.tool_name,
               es.tool_input,
               es.status,
               es.started_at,
               es.completed_at,
               es.duration_ms,
               es.error_message
        FROM execution_steps es
        WHERE es.incident_id = %s AND es.org_id = %s
        ORDER BY es.step_index ASC
    """

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[WATERFALL]")
                cur.execute(query, (incident_id, org_id))
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, row)) for row in cur.fetchall()]

        total_steps = len(rows)
        total_duration_ms = sum(r["duration_ms"] or 0 for r in rows)
        error_count = sum(1 for r in rows if r["status"] == "error")

        for row in rows:
            for k, v in row.items():
                if hasattr(v, "isoformat"):
                    row[k] = v.isoformat()
            if row.get("tool_input") and not isinstance(row["tool_input"], str):
                pass  # psycopg2 returns JSONB as dict already

        return jsonify({
            "incident_id": incident_id,
            "total_steps": total_steps,
            "total_duration_ms": total_duration_ms,
            "error_count": error_count,
            "steps": rows,
        }), 200
    except Exception:
        logger.exception("incident_waterfall failed")
        return jsonify({"error": "Failed to fetch waterfall data"}), 500


@waterfall_bp.route("/api/monitor/tools/performance", methods=["GET"])
@require_permission("incidents", "read")
def tool_performance(user_id):
    """Aggregated tool-level stats: call count, avg/p95 duration, success rate."""
    org_id = get_org_id_from_request()
    time_range = request.args.get("time_range", "7d")
    interval_map = {"1d": "1 day", "7d": "7 days", "30d": "30 days", "90d": "90 days"}
    pg_interval = interval_map.get(time_range, "7 days")

    query = """
        SELECT es.tool_name,
               COUNT(*) AS call_count,
               ROUND(AVG(es.duration_ms)::numeric, 1) AS avg_duration_ms,
               PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY es.duration_ms)
                   AS p95_duration_ms,
               ROUND(
                   COUNT(*) FILTER (WHERE es.status = 'success')::numeric
                   / NULLIF(COUNT(*)::numeric, 0) * 100, 1
               ) AS success_rate,
               COUNT(*) FILTER (WHERE es.status = 'error') AS error_count
        FROM execution_steps es
        WHERE es.org_id = %s
          AND es.started_at >= NOW() - %s::interval
        GROUP BY es.tool_name
        ORDER BY call_count DESC
    """

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[TOOL_PERF]")
                cur.execute(query, (org_id, pg_interval))
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, row)) for row in cur.fetchall()]

        for row in rows:
            for k, v in row.items():
                if hasattr(v, "isoformat"):
                    row[k] = v.isoformat()
                elif isinstance(v, type(None)):
                    pass
                elif hasattr(v, "__float__"):
                    row[k] = float(v)

        return jsonify(rows), 200
    except Exception:
        logger.exception("tool_performance failed")
        return jsonify({"error": "Failed to fetch tool performance"}), 500
