"""SRE metrics API routes — MTTR, MTTD, Change Failure Rate, Incident Frequency, Agent Execution."""

import logging
from flask import Blueprint, jsonify, request
from utils.db.connection_pool import db_pool
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request, set_rls_context

logger = logging.getLogger(__name__)

metrics_bp = Blueprint("metrics", __name__)

# Valid period values and their PostgreSQL interval strings
_PERIOD_MAP = {
    "7d": "7 days",
    "30d": "30 days",
    "90d": "90 days",
    "180d": "180 days",
    "365d": "365 days",
}


def _get_period_interval(period_str: str) -> str:
    return _PERIOD_MAP.get(period_str, "30 days")


def _parse_window_hours(default: int = 4) -> tuple[int | None, tuple | None]:
    """Parse and validate the window_hours query parameter.

    Returns (value, None) on success or (None, (response, status)) on failure
    so the caller can do `if err: return err`.
    """
    raw = request.args.get("window_hours", str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None, (jsonify({"error": "window_hours must be a positive integer"}), 400)
    if value < 1:
        return None, (jsonify({"error": "window_hours must be a positive integer"}), 400)
    return value, None


@metrics_bp.route("/api/metrics/summary", methods=["GET"])
@require_permission("incidents", "read")
def get_metrics_summary(user_id):
    """Dashboard overview — key SRE metrics in a single call."""
    org_id = get_org_id_from_request()
    period = _get_period_interval(request.args.get("period", "30d"))
    window_hours, err = _parse_window_hours()
    if err:
        return err

    try:
        with db_pool.get_user_connection() as conn:
            cursor = conn.cursor()
            set_rls_context(cursor, conn, user_id, log_prefix="[Metrics]")

            # Total / active / resolved counts
            cursor.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE started_at >= NOW() - %s::interval) as total,
                    COUNT(*) FILTER (WHERE status IN ('investigating', 'analyzed') AND aurora_status NOT IN ('complete', 'resolved')) as active,
                    COUNT(*) FILTER (WHERE
                        status = 'resolved'
                        AND resolved_at >= NOW() - %s::interval
                    ) as resolved,
                    COUNT(*) FILTER (WHERE
                        analyzed_at IS NOT NULL
                        AND analyzed_at >= NOW() - %s::interval
                    ) as analyzed
                FROM incidents
            """, (period, period, period))
            counts = cursor.fetchone()
            total_incidents = counts[0] or 0
            active_incidents = counts[1] or 0
            resolved_incidents = counts[2] or 0
            analyzed_incidents = counts[3] or 0

            # Avg MTTR (seconds) — only incidents explicitly resolved by a human.
            cursor.execute("""
                SELECT AVG(EXTRACT(EPOCH FROM (
                    COALESCE(resolved_at, analyzed_at) - started_at
                )))
                FROM incidents
                WHERE resolved_at IS NOT NULL
                  AND status = 'resolved'
                  AND resolved_at >= NOW() - %s::interval
            """, (period,))
            avg_mttr = cursor.fetchone()[0]

            # Avg MTTS (seconds) — Mean Time to Solution: how fast Aurora
            # produces an RCA (analyzed_at - started_at).
            cursor.execute("""
                SELECT AVG(EXTRACT(EPOCH FROM (analyzed_at - started_at)))
                FROM incidents
                WHERE analyzed_at IS NOT NULL
                  AND analyzed_at >= NOW() - %s::interval
            """, (period,))
            avg_mtts = cursor.fetchone()[0]

            # Avg MTTD (seconds) — fall back to created_at vs started_at for
            # webhook-ingested alerts where alert_fired_at isn't populated.
            cursor.execute("""
                SELECT AVG(EXTRACT(EPOCH FROM (investigation_started_at - started_at)))
                FROM incidents
                WHERE investigation_started_at IS NOT NULL
                  AND started_at >= NOW() - %s::interval
                  AND investigation_started_at >= started_at
            """, (period,))
            avg_mttd = cursor.fetchone()[0]

            # Change Failure Rate (window_hours validated above)
            cursor.execute("""
                WITH deploys AS (
                    SELECT id, service, received_at
                    FROM jenkins_deployment_events
                    WHERE received_at >= NOW() - %s::interval
                ),
                deploy_failures AS (
                    SELECT DISTINCT d.id
                    FROM deploys d
                    JOIN incidents i ON (
                        i.alert_service = d.service
                        AND i.started_at BETWEEN d.received_at AND d.received_at + make_interval(hours => %s)
                        AND i.status != 'merged'
                    )
                )
                SELECT
                    (SELECT COUNT(*) FROM deploys) as total,
                    (SELECT COUNT(*) FROM deploy_failures) as failures
            """, (period, window_hours))
            cfr_row = cursor.fetchone()
            total_deploys = cfr_row[0] or 0
            failed_deploys = cfr_row[1] or 0
            cfr = (failed_deploys / total_deploys * 100) if total_deploys > 0 else 0

            # Top affected services
            cursor.execute("""
                SELECT alert_service, COUNT(*) as cnt
                FROM incidents
                WHERE started_at >= NOW() - %s::interval
                  AND alert_service IS NOT NULL
                  AND status != 'merged'
                GROUP BY alert_service
                ORDER BY cnt DESC
                LIMIT 10
            """, (period,))
            top_services = [{"service": r[0], "count": r[1]} for r in cursor.fetchall()]

        return jsonify({
            "totalIncidents": total_incidents,
            "activeIncidents": active_incidents,
            "resolvedIncidents": resolved_incidents,
            "analyzedIncidents": analyzed_incidents,
            "avgMttrSeconds": round(avg_mttr, 1) if avg_mttr else None,
            "avgMttsSeconds": round(avg_mtts, 1) if avg_mtts else None,
            "avgMttdSeconds": round(avg_mttd, 1) if avg_mttd else None,
            "changeFailureRate": round(cfr, 2),
            "totalDeployments": total_deploys,
            "topServices": top_services,
        })

    except Exception as e:
        logger.exception("[METRICS] Error computing summary for user %s: %s", user_id, e)
        return jsonify({"error": "Failed to compute metrics summary"}), 500


@metrics_bp.route("/api/metrics/mttr", methods=["GET"])
@require_permission("incidents", "read")
def get_mttr(user_id):
    """Mean Time to Resolve — only incidents explicitly marked resolved by a human."""
    org_id = get_org_id_from_request()
    period = _get_period_interval(request.args.get("period", "30d"))
    severity_filter = request.args.get("severity")
    service_filter = request.args.get("service")

    try:
        with db_pool.get_user_connection() as conn:
            cursor = conn.cursor()
            set_rls_context(cursor, conn, user_id, log_prefix="[Metrics]")

            where_clauses = [
                "resolved_at IS NOT NULL",
                "status = 'resolved'",
                "resolved_at >= NOW() - %s::interval",
            ]
            params = [period]

            if severity_filter:
                where_clauses.append("severity = %s")
                params.append(severity_filter)
            if service_filter:
                where_clauses.append("alert_service = %s")
                params.append(service_filter)

            where_sql = " AND ".join(where_clauses)

            # By severity. The "investigation end" used everywhere here is
            # COALESCE(resolved_at, analyzed_at) so the math is consistent
            # whether the incident was human-resolved or just RCA-completed.
            cursor.execute(f"""
                SELECT
                    COALESCE(severity, 'unknown') as severity,
                    COUNT(*) as count,
                    AVG(EXTRACT(EPOCH FROM (COALESCE(resolved_at, analyzed_at) - started_at))) as avg_mttr,
                    PERCENTILE_CONT(0.5) WITHIN GROUP (
                        ORDER BY EXTRACT(EPOCH FROM (COALESCE(resolved_at, analyzed_at) - started_at))
                    ) as p50_mttr,
                    PERCENTILE_CONT(0.95) WITHIN GROUP (
                        ORDER BY EXTRACT(EPOCH FROM (COALESCE(resolved_at, analyzed_at) - started_at))
                    ) as p95_mttr,
                    AVG(EXTRACT(EPOCH FROM (
                        COALESCE(analyzed_at, resolved_at) - started_at
                    ))) as avg_detection_to_rca,
                    AVG(EXTRACT(EPOCH FROM (
                        COALESCE(resolved_at, analyzed_at) - COALESCE(analyzed_at, started_at)
                    ))) as avg_rca_to_resolve
                FROM incidents
                WHERE {where_sql}
                GROUP BY severity
                ORDER BY count DESC
            """, params)

            by_severity = []
            for row in cursor.fetchall():
                by_severity.append({
                    "severity": row[0],
                    "count": row[1],
                    "avgMttrSeconds": round(row[2], 1) if row[2] else None,
                    "p50MttrSeconds": round(row[3], 1) if row[3] else None,
                    "p95MttrSeconds": round(row[4], 1) if row[4] else None,
                    "avgDetectionToRcaSeconds": round(row[5], 1) if row[5] else None,
                    "avgRcaToResolveSeconds": round(row[6], 1) if row[6] else None,
                })

            # Time series (daily) — bucket by the same effective end timestamp.
            cursor.execute(f"""
                SELECT
                    date_trunc('day', COALESCE(resolved_at, analyzed_at))::date as day,
                    AVG(EXTRACT(EPOCH FROM (COALESCE(resolved_at, analyzed_at) - started_at))) as avg_mttr,
                    COUNT(*) as count
                FROM incidents
                WHERE {where_sql}
                GROUP BY day
                ORDER BY day ASC
            """, params)

            trend = [
                {"date": str(row[0]), "avgMttrSeconds": round(row[1], 1) if row[1] else None, "count": row[2]}
                for row in cursor.fetchall()
            ]

        return jsonify({"bySeverity": by_severity, "trend": trend})

    except Exception as e:
        logger.exception("[METRICS] Error computing MTTR: %s", e)
        return jsonify({"error": "Failed to compute MTTR"}), 500


@metrics_bp.route("/api/metrics/mtts", methods=["GET"])
@require_permission("incidents", "read")
def get_mtts(user_id):
    """Mean Time to Solution — how fast Aurora produces an RCA (analyzed_at - started_at)."""
    org_id = get_org_id_from_request()
    period = _get_period_interval(request.args.get("period", "30d"))
    severity_filter = request.args.get("severity")
    service_filter = request.args.get("service")

    try:
        with db_pool.get_user_connection() as conn:
            cursor = conn.cursor()
            set_rls_context(cursor, conn, user_id, log_prefix="[Metrics]")

            where_clauses = [
                "analyzed_at IS NOT NULL",
                "analyzed_at >= NOW() - %s::interval",
            ]
            params = [period]

            if severity_filter:
                where_clauses.append("severity = %s")
                params.append(severity_filter)
            if service_filter:
                where_clauses.append("alert_service = %s")
                params.append(service_filter)

            where_sql = " AND ".join(where_clauses)

            # By severity
            cursor.execute(f"""
                SELECT
                    COALESCE(severity, 'unknown') as severity,
                    COUNT(*) as count,
                    AVG(EXTRACT(EPOCH FROM (analyzed_at - started_at))) as avg_mtts,
                    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (analyzed_at - started_at))) as p50_mtts,
                    PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (analyzed_at - started_at))) as p95_mtts
                FROM incidents
                WHERE {where_sql}
                GROUP BY severity
                ORDER BY count DESC
            """, params)

            by_severity = []
            for row in cursor.fetchall():
                by_severity.append({
                    "severity": row[0],
                    "count": row[1],
                    "avgMttsSeconds": round(row[2], 1) if row[2] else None,
                    "p50MttsSeconds": round(row[3], 1) if row[3] else None,
                    "p95MttsSeconds": round(row[4], 1) if row[4] else None,
                })

            # Time series (daily)
            cursor.execute(f"""
                SELECT
                    date_trunc('day', analyzed_at)::date as day,
                    AVG(EXTRACT(EPOCH FROM (analyzed_at - started_at))) as avg_mtts,
                    COUNT(*) as count
                FROM incidents
                WHERE {where_sql}
                GROUP BY day
                ORDER BY day ASC
            """, params)

            trend = [
                {"date": str(row[0]), "avgMttsSeconds": round(row[1], 1) if row[1] else None, "count": row[2]}
                for row in cursor.fetchall()
            ]

        return jsonify({"bySeverity": by_severity, "trend": trend})

    except Exception as e:
        logger.exception("[METRICS] Error computing MTTS: %s", e)
        return jsonify({"error": "Failed to compute MTTS"}), 500


@metrics_bp.route("/api/metrics/mttd", methods=["GET"])
@require_permission("incidents", "read")
def get_mttd(user_id):
    """MTTD = pickup latency — time from webhook arrival (started_at) to the
    moment the RCA worker actually began running (investigation_started_at).
    """
    org_id = get_org_id_from_request()
    period = _get_period_interval(request.args.get("period", "30d"))

    try:
        with db_pool.get_user_connection() as conn:
            cursor = conn.cursor()
            set_rls_context(cursor, conn, user_id, log_prefix="[Metrics]")

            cursor.execute("""
                SELECT
                    source_type,
                    COUNT(*) as count,
                    AVG(EXTRACT(EPOCH FROM (investigation_started_at - started_at))) as avg_mttd,
                    PERCENTILE_CONT(0.5) WITHIN GROUP (
                        ORDER BY EXTRACT(EPOCH FROM (investigation_started_at - started_at))
                    ) as p50_mttd,
                    PERCENTILE_CONT(0.95) WITHIN GROUP (
                        ORDER BY EXTRACT(EPOCH FROM (investigation_started_at - started_at))
                    ) as p95_mttd
                FROM incidents
                WHERE investigation_started_at IS NOT NULL
                  AND started_at >= NOW() - %s::interval
                  AND investigation_started_at >= started_at
                GROUP BY source_type
                ORDER BY count DESC
            """, (period,))

            by_source = []
            for row in cursor.fetchall():
                by_source.append({
                    "sourceType": row[0],
                    "count": row[1],
                    "avgMttdSeconds": round(row[2], 1) if row[2] else None,
                    "p50MttdSeconds": round(row[3], 1) if row[3] else None,
                    "p95MttdSeconds": round(row[4], 1) if row[4] else None,
                })

        return jsonify({"bySource": by_source})

    except Exception as e:
        logger.exception("[METRICS] Error computing MTTD: %s", e)
        return jsonify({"error": "Failed to compute MTTD"}), 500


@metrics_bp.route("/api/metrics/incident-frequency", methods=["GET"])
@require_permission("incidents", "read")
def get_incident_frequency(user_id):
    """Incident count over time, grouped by severity or service."""
    org_id = get_org_id_from_request()
    period = _get_period_interval(request.args.get("period", "30d"))
    group_by = request.args.get("group_by", "severity")

    if group_by not in ("severity", "service", "source_type"):
        group_by = "severity"

    group_col = "alert_service" if group_by == "service" else group_by

    try:
        with db_pool.get_user_connection() as conn:
            cursor = conn.cursor()
            set_rls_context(cursor, conn, user_id, log_prefix="[Metrics]")

            cursor.execute(f"""
                SELECT
                    date_trunc('day', started_at)::date as day,
                    COALESCE({group_col}, 'unknown') as group_value,
                    COUNT(*) as count
                FROM incidents
                WHERE started_at >= NOW() - %s::interval
                  AND status != 'merged'
                GROUP BY day, group_value
                ORDER BY day ASC, count DESC
            """, (period,))

            data = [
                {"date": str(row[0]), "group": row[1], "count": row[2]}
                for row in cursor.fetchall()
            ]

        return jsonify({"data": data, "groupBy": group_by})

    except Exception as e:
        logger.exception("[METRICS] Error computing incident frequency: %s", e)
        return jsonify({"error": "Failed to compute incident frequency"}), 500


@metrics_bp.route("/api/metrics/change-failure-rate", methods=["GET"])
@require_permission("incidents", "read")
def get_change_failure_rate(user_id):
    """Percentage of deployments followed by an incident within a time window."""
    org_id = get_org_id_from_request()
    period = _get_period_interval(request.args.get("period", "30d"))
    window_hours, err = _parse_window_hours()
    if err:
        return err

    try:
        with db_pool.get_user_connection() as conn:
            cursor = conn.cursor()
            set_rls_context(cursor, conn, user_id, log_prefix="[Metrics]")

            cursor.execute("""
                WITH deploys AS (
                    SELECT id, service, received_at
                    FROM jenkins_deployment_events
                    WHERE received_at >= NOW() - %s::interval
                ),
                deploy_failures AS (
                    SELECT DISTINCT d.id as deploy_id, d.service
                    FROM deploys d
                    JOIN incidents i ON (
                        i.alert_service = d.service
                        AND i.started_at BETWEEN d.received_at AND d.received_at + make_interval(hours => %s)
                        AND i.status != 'merged'
                    )
                )
                SELECT
                    d.service,
                    COUNT(DISTINCT d.id) as total_deployments,
                    COUNT(DISTINCT df.deploy_id) as failure_linked
                FROM deploys d
                LEFT JOIN deploy_failures df ON d.id = df.deploy_id AND d.service = df.service
                WHERE d.service IS NOT NULL
                GROUP BY d.service
                ORDER BY total_deployments DESC
            """, (period, window_hours))

            by_service = []
            total_all = 0
            failures_all = 0
            for row in cursor.fetchall():
                total = row[1]
                failures = row[2]
                total_all += total
                failures_all += failures
                by_service.append({
                    "service": row[0],
                    "totalDeployments": total,
                    "failureLinked": failures,
                    "rate": round(failures / total * 100, 2) if total > 0 else 0,
                })

            overall_rate = round(failures_all / total_all * 100, 2) if total_all > 0 else 0

        return jsonify({
            "totalDeployments": total_all,
            "failureLinked": failures_all,
            "changeFailureRate": overall_rate,
            "windowHours": window_hours,
            "byService": by_service,
        })

    except Exception as e:
        logger.exception("[METRICS] Error computing change failure rate: %s", e)
        return jsonify({"error": "Failed to compute change failure rate"}), 500


@metrics_bp.route("/api/metrics/agent-execution", methods=["GET"])
@require_permission("incidents", "read")
def get_agent_execution(user_id):
    """Agent execution waterfall (per-incident) or aggregate tool stats."""
    org_id = get_org_id_from_request()
    period = _get_period_interval(request.args.get("period", "30d"))
    incident_id = request.args.get("incident_id")

    try:
        with db_pool.get_user_connection() as conn:
            cursor = conn.cursor()
            set_rls_context(cursor, conn, user_id, log_prefix="[Metrics]")

            if incident_id:
                # Per-incident waterfall
                cursor.execute("""
                    SELECT step_type, occurred_at, tool_name, command, content
                    FROM (
                        SELECT
                            'thought' as step_type,
                            t.timestamp as occurred_at,
                            t.thought_type as tool_name,
                            NULL as command,
                            t.content
                        FROM incident_thoughts t
                        WHERE t.incident_id = %s

                        UNION ALL

                        SELECT
                            'tool_call' as step_type,
                            c.executed_at as occurred_at,
                            c.tool_name,
                            c.command,
                            c.output as content
                        FROM incident_citations c
                        WHERE c.incident_id = %s
                    ) combined
                    ORDER BY occurred_at ASC NULLS LAST
                """, (incident_id, incident_id))

                steps = []
                for row in cursor.fetchall():
                    steps.append({
                        "type": row[0],
                        "timestamp": row[1].isoformat() + "Z" if row[1] else None,
                        "toolName": row[2],
                        "command": row[3],
                        "content": row[4][:500] if row[4] else None,
                    })

                # Get lifecycle events for this incident
                cursor.execute("""
                    SELECT event_type, previous_value, new_value, created_at
                    FROM incident_lifecycle_events
                    WHERE incident_id = %s
                    ORDER BY created_at ASC
                """, (incident_id,))

                lifecycle = [
                    {
                        "eventType": row[0],
                        "previousValue": row[1],
                        "newValue": row[2],
                        "timestamp": row[3].isoformat() + "Z" if row[3] else None,
                    }
                    for row in cursor.fetchall()
                ]

                return jsonify({"steps": steps, "lifecycle": lifecycle})

            else:
                # Aggregate tool stats
                cursor.execute("""
                    SELECT
                        c.tool_name,
                        COUNT(*) as total_calls,
                        COUNT(DISTINCT c.incident_id) as incidents_used
                    FROM incident_citations c
                    JOIN incidents i ON c.incident_id = i.id
                    WHERE i.started_at >= NOW() - %s::interval
                      AND c.tool_name IS NOT NULL
                    GROUP BY c.tool_name
                    ORDER BY total_calls DESC
                """, (period,))

                tool_stats = [
                    {"toolName": row[0], "totalCalls": row[1], "incidentsUsed": row[2]}
                    for row in cursor.fetchall()
                ]

                # Avg steps per RCA
                cursor.execute("""
                    SELECT AVG(step_count)
                    FROM (
                        SELECT
                            i.id,
                            (SELECT COUNT(*) FROM incident_thoughts t WHERE t.incident_id = i.id) +
                            (SELECT COUNT(*) FROM incident_citations c WHERE c.incident_id = i.id) as step_count
                        FROM incidents i
                        WHERE i.aurora_status = 'complete'
                          AND i.started_at >= NOW() - %s::interval
                    ) sub
                    WHERE step_count > 0
                """, (period,))
                avg_steps = cursor.fetchone()[0]

                # Total RCAs completed
                cursor.execute("""
                    SELECT COUNT(*)
                    FROM incidents
                    WHERE aurora_status = 'complete'
                      AND started_at >= NOW() - %s::interval
                """, (period,))
                total_rcas = cursor.fetchone()[0] or 0

                return jsonify({
                    "toolStats": tool_stats,
                    "avgStepsPerRca": round(avg_steps, 1) if avg_steps else None,
                    "totalRcasCompleted": total_rcas,
                })

    except Exception as e:
        logger.exception("[METRICS] Error computing agent execution: %s", e)
        return jsonify({"error": "Failed to compute agent execution metrics"}), 500
