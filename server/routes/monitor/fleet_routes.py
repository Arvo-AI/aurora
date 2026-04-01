"""Fleet routes -- agent-run list, summary counts, per-incident activity drill-down."""
import logging
from flask import Blueprint, request, jsonify
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request, set_rls_context
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

fleet_bp = Blueprint("monitor_fleet", __name__)


@fleet_bp.route("/api/monitor/fleet", methods=["GET"])
@require_permission("incidents", "read")
def fleet_list(user_id):
    """List agent runs (incidents that have an associated chat session)."""
    org_id = get_org_id_from_request()

    status_filter = request.args.get("status")
    environment_filter = request.args.get("environment")
    service_filter = request.args.get("service")
    time_range = request.args.get("time_range", "7d")

    interval_map = {"1d": "1 day", "7d": "7 days", "30d": "30 days", "90d": "90 days"}
    pg_interval = interval_map.get(time_range, "7 days")

    conditions = ["i.org_id = %s", "cs.id IS NOT NULL", "i.created_at >= NOW() - %s::interval"]
    params: list = [org_id, pg_interval]

    if status_filter:
        conditions.append("i.aurora_status = %s")
        params.append(status_filter)
    if environment_filter:
        conditions.append("i.alert_environment = %s")
        params.append(environment_filter)
    if service_filter:
        conditions.append("i.alert_service = %s")
        params.append(service_filter)

    where = " AND ".join(conditions)

    query = f"""
        SELECT i.id AS incident_id,
               i.alert_service,
               i.alert_environment,
               i.aurora_status,
               i.severity,
               i.source_type,
               i.created_at AS started_at,
               i.analyzed_at,
               i.updated_at,
               cs.id AS session_id
        FROM incidents i
        LEFT JOIN chat_sessions cs ON cs.id = i.aurora_chat_session_id::text
        WHERE {where}
        ORDER BY i.created_at DESC
        LIMIT 200
    """

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[FLEET]")
                cur.execute(query, params)
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, row)) for row in cur.fetchall()]

        for row in rows:
            for k, v in row.items():
                if hasattr(v, "isoformat"):
                    row[k] = v.isoformat()
                elif isinstance(v, bytes):
                    row[k] = v.hex()

        return jsonify(rows), 200
    except Exception:
        logger.exception("fleet_list failed")
        return jsonify({"error": "Failed to fetch fleet data"}), 500


@fleet_bp.route("/api/monitor/fleet/summary", methods=["GET"])
@require_permission("incidents", "read")
def fleet_summary(user_id):
    """Aggregated fleet summary: counts by status, avg RCA duration, active count."""
    org_id = get_org_id_from_request()

    query = """
        SELECT
            COUNT(*) FILTER (WHERE cs.id IS NOT NULL) AS total_agent_runs,
            COUNT(*) FILTER (WHERE i.aurora_status IN ('running', 'analyzing', 'pending')) AS active_count,
            COUNT(*) FILTER (WHERE i.aurora_status IN ('complete', 'completed', 'resolved', 'analyzed')) AS completed_count,
            COUNT(*) FILTER (WHERE i.aurora_status = 'error') AS error_count,
            AVG(EXTRACT(EPOCH FROM (i.analyzed_at - i.created_at)))
                FILTER (WHERE i.analyzed_at IS NOT NULL) AS avg_rca_duration_seconds
        FROM incidents i
        LEFT JOIN chat_sessions cs ON cs.id = i.aurora_chat_session_id::text
        WHERE i.org_id = %s
    """

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[FLEET_SUMMARY]")
                cur.execute(query, (org_id,))
                row = cur.fetchone()

        result = {
            "total_agent_runs": row[0] or 0,
            "active_count": row[1] or 0,
            "completed_count": row[2] or 0,
            "error_count": row[3] or 0,
            "avg_rca_duration_seconds": round(float(row[4]), 2) if row[4] else None,
        }
        return jsonify(result), 200
    except Exception:
        logger.exception("fleet_summary failed")
        return jsonify({"error": "Failed to fetch fleet summary"}), 500


@fleet_bp.route("/api/monitor/fleet/<incident_id>/activity", methods=["GET"])
@require_permission("incidents", "read")
def fleet_activity(user_id, incident_id):
    """Chronological union of execution_steps + incident_thoughts + incident_citations for one incident."""
    org_id = get_org_id_from_request()

    query = """
        (
            SELECT 'execution_step' AS event_type,
                   es.tool_name AS label,
                   es.status,
                   es.started_at AS event_time,
                   es.duration_ms,
                   es.tool_input::text AS detail,
                   es.error_message
            FROM execution_steps es
            WHERE es.incident_id = %s AND es.org_id = %s
        )
        UNION ALL
        (
            SELECT 'thought' AS event_type,
                   it.thought_type AS label,
                   'complete' AS status,
                   it.created_at AS event_time,
                   NULL AS duration_ms,
                   LEFT(it.content, 500) AS detail,
                   NULL AS error_message
            FROM incident_thoughts it
            WHERE it.incident_id = %s
        )
        UNION ALL
        (
            SELECT 'citation' AS event_type,
                   ic.tool_name AS label,
                   'complete' AS status,
                   ic.executed_at AS event_time,
                   NULL AS duration_ms,
                   ic.citation_key AS detail,
                   NULL AS error_message
            FROM incident_citations ic
            WHERE ic.incident_id = %s
        )
        ORDER BY event_time ASC NULLS LAST
    """

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[FLEET_ACTIVITY]")
                cur.execute(query, (incident_id, org_id, incident_id, incident_id))
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, row)) for row in cur.fetchall()]

        for row in rows:
            for k, v in row.items():
                if hasattr(v, "isoformat"):
                    row[k] = v.isoformat()

        return jsonify(rows), 200
    except Exception:
        logger.exception("fleet_activity failed")
        return jsonify({"error": "Failed to fetch activity"}), 500
