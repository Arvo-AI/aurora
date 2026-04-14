"""Fleet routes -- agent-run list, summary counts, per-incident activity drill-down."""
import logging
import uuid
from flask import Blueprint, request, jsonify
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request, set_rls_context
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

fleet_bp = Blueprint("monitor_fleet", __name__)


@fleet_bp.route("/api/monitor/fleet", methods=["GET"])
@require_permission("incidents", "read")
def fleet_list(user_id):
    """List agent runs with rich incident context."""
    org_id = get_org_id_from_request()

    status_filter = request.args.get("status")
    service_filter = request.args.get("service")
    time_range = request.args.get("time_range", "7d")

    interval_map = {"1d": "1 day", "7d": "7 days", "30d": "30 days", "90d": "90 days"}
    if time_range not in interval_map:
        return jsonify({"error": f"Unsupported time_range '{time_range}'. Must be one of: {', '.join(sorted(interval_map))}"}), 400
    pg_interval = interval_map[time_range]

    conditions = ["i.org_id = %s", "i.created_at >= NOW() - %s::interval"]
    params: list = [org_id, pg_interval]

    if status_filter:
        conditions.append("i.aurora_status = %s")
        params.append(status_filter)
    if service_filter:
        conditions.append("i.alert_service = %s")
        params.append(service_filter)

    where = " AND ".join(conditions)

    query = f"""
        SELECT i.id AS incident_id,
               i.alert_title,
               i.alert_service,
               i.aurora_status,
               i.severity,
               i.source_type,
               i.created_at AS started_at,
               i.analyzed_at,
               i.updated_at,
               i.status AS incident_status,
               i.aurora_summary,
               EXTRACT(EPOCH FROM (
                   COALESCE(i.analyzed_at, i.updated_at) - i.created_at
               )) AS duration_seconds,
               cs.id AS session_id,
               (SELECT COUNT(*) FROM incident_suggestions s WHERE s.incident_id = i.id) AS suggestion_count,
               (SELECT string_agg(s.title, ' | ' ORDER BY s.id) FROM incident_suggestions s WHERE s.incident_id = i.id AND s.type = 'fix') AS fix_titles,
               (SELECT string_agg(s.title, ' | ' ORDER BY s.id) FROM incident_suggestions s WHERE s.incident_id = i.id AND s.type = 'diagnostic') AS diagnostic_titles,
               (SELECT string_agg(s.title, ' | ' ORDER BY s.id) FROM incident_suggestions s WHERE s.incident_id = i.id AND s.type = 'mitigation') AS mitigation_titles,
               i.correlated_alert_count
        FROM incidents i
        JOIN chat_sessions cs ON cs.id = i.aurora_chat_session_id::text
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
            if row.get("duration_seconds") is not None:
                row["duration_seconds"] = round(float(row["duration_seconds"]), 1)

        return jsonify(rows), 200
    except Exception:
        logger.exception("fleet_list failed")
        return jsonify({"error": "Failed to fetch fleet data"}), 500


@fleet_bp.route("/api/monitor/fleet/summary", methods=["GET"])
@require_permission("incidents", "read")
def fleet_summary(user_id):
    """Aggregated fleet summary: counts by status, avg RCA duration, active count."""
    org_id = get_org_id_from_request()

    time_range = request.args.get("time_range", "30d")
    interval_map = {"1d": "1 day", "7d": "7 days", "30d": "30 days", "90d": "90 days"}
    if time_range not in interval_map:
        return jsonify({"error": f"Unsupported time_range '{time_range}'. Must be one of: {', '.join(sorted(interval_map))}"}), 400
    pg_interval = interval_map[time_range]

    query = """
        SELECT
            COUNT(*) AS total_agent_runs,
            COUNT(*) FILTER (WHERE i.aurora_status IN ('running', 'analyzing', 'summarizing', 'pending')) AS active_count,
            COUNT(*) FILTER (WHERE i.aurora_status IN ('complete', 'completed', 'resolved', 'analyzed')) AS completed_count,
            COUNT(*) FILTER (WHERE i.aurora_status = 'error') AS error_count,
            AVG(EXTRACT(EPOCH FROM (i.analyzed_at - i.created_at)))
                FILTER (WHERE i.analyzed_at IS NOT NULL) AS avg_rca_duration_seconds,
            COUNT(*) FILTER (WHERE i.severity = 'critical') AS critical_count,
            COUNT(*) FILTER (WHERE i.severity = 'high') AS high_count
        FROM incidents i
        JOIN chat_sessions cs ON cs.id = i.aurora_chat_session_id::text
        WHERE i.org_id = %s AND i.created_at >= NOW() - %s::interval
    """

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[FLEET_SUMMARY]")
                cur.execute(query, (org_id, pg_interval))
                row = cur.fetchone()

        result = {
            "total_agent_runs": row[0] or 0,
            "active_count": row[1] or 0,
            "completed_count": row[2] or 0,
            "error_count": row[3] or 0,
            "avg_rca_duration_seconds": round(float(row[4]), 2) if row[4] is not None else None,
            "critical_count": row[5] or 0,
            "high_count": row[6] or 0,
        }
        return jsonify(result), 200
    except Exception:
        logger.exception("fleet_summary failed")
        return jsonify({"error": "Failed to fetch fleet summary"}), 500


@fleet_bp.route("/api/monitor/fleet/<incident_id>/activity", methods=["GET"])
@require_permission("incidents", "read")
def fleet_activity(user_id, incident_id):
    """Chronological union of execution_steps + incident_thoughts + incident_citations for one incident."""
    try:
        uuid.UUID(incident_id)
    except (ValueError, AttributeError):
        return jsonify({"error": "Invalid incident_id format"}), 400

    org_id = get_org_id_from_request()

    query = """
        SELECT * FROM (
            (
                SELECT 'execution_step' AS event_type,
                       es.tool_name AS label,
                       es.status,
                       es.started_at AS event_time,
                       es.duration_ms,
                       LEFT(es.tool_input::text, 500) AS detail,
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
                JOIN incidents i ON i.id = it.incident_id
                WHERE it.incident_id = %s AND i.org_id = %s
            )
            UNION ALL
            (
                SELECT 'citation' AS event_type,
                       ic.tool_name AS label,
                       COALESCE(ic.status, 'success') AS status,
                       ic.executed_at AS event_time,
                       ic.duration_ms,
                       ic.citation_key AS detail,
                       ic.error_message
                FROM incident_citations ic
                JOIN incidents i ON i.id = ic.incident_id
                WHERE ic.incident_id = %s AND i.org_id = %s
            )
            ORDER BY event_time DESC NULLS LAST
            LIMIT 500
        ) recent
        ORDER BY event_time ASC NULLS LAST
    """

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[FLEET_ACTIVITY]")
                cur.execute(query, (incident_id, org_id, incident_id, org_id, incident_id, org_id))
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
