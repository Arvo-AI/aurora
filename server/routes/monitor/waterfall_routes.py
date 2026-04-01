"""Waterfall routes -- per-incident execution timeline + aggregated tool/RCA stats.

Pulls from three data sources:
  - incident_citations: tool calls with outputs (the primary source of "what the agent did")
  - execution_steps: real-time timing data (duration_ms, status) captured during tool execution
  - incident_thoughts: agent reasoning steps
  - llm_usage_tracking: LLM calls with token counts, costs, response times
"""
import logging
from flask import Blueprint, request, jsonify
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request, set_rls_context
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

waterfall_bp = Blueprint("monitor_waterfall", __name__)


@waterfall_bp.route("/api/monitor/incidents/<incident_id>/timeline", methods=["GET"])
@require_permission("incidents", "read")
def incident_timeline(user_id, incident_id):
    """Full execution timeline for one incident: tool calls, thoughts, LLM calls merged chronologically."""
    org_id = get_org_id_from_request()

    query = """
        WITH timeline AS (
            -- Tool calls from citations, enriched with execution_steps timing
            (
                SELECT 'tool_call' AS event_type,
                       ic.tool_name AS label,
                       ic.command AS detail,
                       LEFT(ic.output, 2000) AS output,
                       NULL::int AS tokens,
                       NULL::numeric AS cost,
                       COALESCE(es.duration_ms, ic.duration_ms) AS response_time_ms,
                       COALESCE(es.status, ic.status, 'success') AS step_status,
                       COALESCE(es.error_message, ic.error_message) AS step_error,
                       ic.executed_at AS event_time,
                       ic.citation_key AS sort_key
                FROM incident_citations ic
                LEFT JOIN execution_steps es
                    ON es.incident_id = ic.incident_id
                   AND es.tool_name = ic.tool_name
                   AND es.step_index = CAST(ic.citation_key AS INTEGER)
                WHERE ic.incident_id = %s
            )
            UNION ALL
            -- Agent reasoning
            (
                SELECT 'thought' AS event_type,
                       it.thought_type AS label,
                       LEFT(it.content, 2000) AS detail,
                       NULL AS output,
                       NULL::int AS tokens,
                       NULL::numeric AS cost,
                       NULL::int AS response_time_ms,
                       NULL AS step_status,
                       NULL AS step_error,
                       COALESCE(it.timestamp, it.created_at) AS event_time,
                       NULL AS sort_key
                FROM incident_thoughts it
                WHERE it.incident_id = %s
            )
            UNION ALL
            -- LLM calls
            (
                SELECT 'llm_call' AS event_type,
                       lu.model_name AS label,
                       lu.request_type AS detail,
                       lu.error_message AS output,
                       lu.total_tokens AS tokens,
                       lu.total_cost_with_surcharge AS cost,
                       lu.response_time_ms,
                       CASE WHEN lu.error_message IS NOT NULL THEN 'error' ELSE 'success' END AS step_status,
                       lu.error_message AS step_error,
                       lu.timestamp AS event_time,
                       NULL AS sort_key
                FROM llm_usage_tracking lu
                WHERE lu.session_id = (
                    SELECT aurora_chat_session_id::text FROM incidents WHERE id = %s
                )
            )
        )
        SELECT * FROM timeline
        ORDER BY event_time ASC NULLS LAST, sort_key ASC NULLS LAST
    """

    summary_query = """
        SELECT
            COUNT(DISTINCT ic.id) AS total_tool_calls,
            COUNT(DISTINCT it.id) AS total_thoughts,
            (SELECT COUNT(*) FROM llm_usage_tracking lu
             WHERE lu.session_id = i.aurora_chat_session_id::text) AS total_llm_calls,
            (SELECT SUM(lu.total_tokens) FROM llm_usage_tracking lu
             WHERE lu.session_id = i.aurora_chat_session_id::text) AS total_tokens,
            (SELECT SUM(lu.total_cost_with_surcharge) FROM llm_usage_tracking lu
             WHERE lu.session_id = i.aurora_chat_session_id::text) AS total_cost,
            (SELECT ROUND(AVG(lu.response_time_ms)) FROM llm_usage_tracking lu
             WHERE lu.session_id = i.aurora_chat_session_id::text) AS avg_llm_response_ms,
            (SELECT COUNT(*) FROM execution_steps es
             WHERE es.incident_id = i.id AND es.status = 'error') AS tool_errors,
            (SELECT ROUND(AVG(es.duration_ms)) FROM execution_steps es
             WHERE es.incident_id = i.id AND es.duration_ms IS NOT NULL) AS avg_tool_duration_ms,
            i.aurora_status,
            i.created_at AS started_at,
            i.analyzed_at AS completed_at,
            EXTRACT(EPOCH FROM (i.analyzed_at - i.created_at))::int AS duration_seconds
        FROM incidents i
        LEFT JOIN incident_citations ic ON ic.incident_id = i.id
        LEFT JOIN incident_thoughts it ON it.incident_id = i.id
        WHERE i.id = %s AND i.org_id = %s
        GROUP BY i.id
    """

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[TIMELINE]")

                cur.execute(query, (incident_id, incident_id, incident_id))
                cols = [d[0] for d in cur.description]
                events = [dict(zip(cols, row)) for row in cur.fetchall()]

                cur.execute(summary_query, (incident_id, org_id))
                summary_row = cur.fetchone()

                # Agent session telemetry for this incident
                cur.execute("""
                    SELECT model_name, detected_provider, provider_mode, use_direct_sdk,
                           temperature, mode, is_background, recursion_limit,
                           context_messages_loaded, context_load_ms,
                           rca_compression_applied, rca_compression_before, rca_compression_after,
                           preflight_compression_applied,
                           middleware_trim_applied, middleware_tokens_before, middleware_tokens_after,
                           time_to_first_token_ms, total_events, total_tokens_streamed,
                           model_turns, tool_calls_count, tool_errors_count,
                           retry_attempts, last_retry_error,
                           total_input_tokens, total_output_tokens, total_llm_calls, total_cost,
                           status, error_message, placeholder_warning, duration_ms,
                           started_at, completed_at
                    FROM agent_sessions
                    WHERE incident_id = %s
                    ORDER BY started_at DESC LIMIT 1
                """, (incident_id,))
                as_row = cur.fetchone()
                agent_session_cols = [d[0] for d in cur.description] if cur.description else []
                agent_session = dict(zip(agent_session_cols, as_row)) if as_row else None

        for ev in events:
            for k, v in ev.items():
                if hasattr(v, "isoformat"):
                    ev[k] = v.isoformat()
                elif isinstance(v, type(None)):
                    pass
                elif hasattr(v, "__float__"):
                    ev[k] = float(v)

        if agent_session:
            for k, v in agent_session.items():
                if hasattr(v, "isoformat"):
                    agent_session[k] = v.isoformat()
                elif isinstance(v, type(None)):
                    pass
                elif hasattr(v, "__float__") and not isinstance(v, (int, float)):
                    agent_session[k] = float(v)

        summary = {}
        if summary_row:
            summary_cols = ["total_tool_calls", "total_thoughts", "total_llm_calls",
                            "total_tokens", "total_cost", "avg_llm_response_ms",
                            "tool_errors", "avg_tool_duration_ms",
                            "aurora_status", "started_at", "completed_at", "duration_seconds"]
            summary = dict(zip(summary_cols, summary_row))
            for k, v in summary.items():
                if hasattr(v, "isoformat"):
                    summary[k] = v.isoformat()
                elif isinstance(v, type(None)):
                    pass
                elif hasattr(v, "__float__"):
                    summary[k] = float(v)

        return jsonify({"incident_id": incident_id, "summary": summary, "events": events, "agent_session": agent_session}), 200
    except Exception:
        logger.exception("incident_timeline failed")
        return jsonify({"error": "Failed to fetch timeline"}), 500


@waterfall_bp.route("/api/monitor/tools/stats", methods=["GET"])
@require_permission("incidents", "read")
def tool_stats(user_id):
    """Aggregated tool-level stats across all incidents: call count, timing, errors."""
    org_id = get_org_id_from_request()
    time_range = request.args.get("time_range", "30d")
    interval_map = {"1d": "1 day", "7d": "7 days", "30d": "30 days", "90d": "90 days"}
    pg_interval = interval_map.get(time_range, "30 days")

    query = """
        SELECT
            ic.tool_name,
            COUNT(*) AS call_count,
            COUNT(DISTINCT ic.incident_id) AS incident_count,
            ROUND(AVG(es.duration_ms)) AS avg_duration_ms,
            ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY es.duration_ms)
                  FILTER (WHERE es.duration_ms IS NOT NULL)) AS p95_duration_ms,
            COUNT(*) FILTER (WHERE COALESCE(es.status, ic.status) = 'error') AS error_count,
            ROUND(100.0 * COUNT(*) FILTER (WHERE COALESCE(es.status, ic.status, 'success') != 'error')
                  / GREATEST(COUNT(*), 1), 1) AS success_rate
        FROM incident_citations ic
        JOIN incidents i ON i.id = ic.incident_id
        LEFT JOIN execution_steps es
            ON es.incident_id = ic.incident_id
           AND es.tool_name = ic.tool_name
           AND es.step_index = CAST(ic.citation_key AS INTEGER)
        WHERE i.org_id = %s
          AND ic.created_at >= NOW() - %s::interval
          AND ic.tool_name IS NOT NULL
        GROUP BY ic.tool_name
        ORDER BY call_count DESC
    """

    rca_stats_query = """
        SELECT
            COUNT(*) AS total_rcas,
            COUNT(*) FILTER (WHERE i.aurora_status IN ('complete', 'completed', 'resolved', 'analyzed')) AS successful_rcas,
            COUNT(*) FILTER (WHERE i.aurora_status = 'error') AS failed_rcas,
            ROUND(AVG(
                (SELECT COUNT(*) FROM incident_citations ic2 WHERE ic2.incident_id = i.id)
            ), 1) AS avg_tool_calls_per_rca,
            ROUND(AVG(
                (SELECT COUNT(*) FROM incident_thoughts it2 WHERE it2.incident_id = i.id)
            ), 1) AS avg_thoughts_per_rca,
            ROUND(AVG(EXTRACT(EPOCH FROM (i.analyzed_at - i.created_at)))
                FILTER (WHERE i.analyzed_at IS NOT NULL), 0) AS avg_rca_duration_seconds,
            ROUND(AVG(
                (SELECT SUM(lu.total_tokens) FROM llm_usage_tracking lu
                 WHERE lu.session_id = i.aurora_chat_session_id::text)
            ), 0) AS avg_tokens_per_rca,
            ROUND(AVG(
                (SELECT SUM(lu.total_cost_with_surcharge) FROM llm_usage_tracking lu
                 WHERE lu.session_id = i.aurora_chat_session_id::text)
            )::numeric, 4) AS avg_cost_per_rca,
            ROUND(AVG(
                (SELECT AVG(es.duration_ms) FROM execution_steps es
                 WHERE es.incident_id = i.id AND es.duration_ms IS NOT NULL)
            ), 0) AS avg_tool_duration_ms
        FROM incidents i
        LEFT JOIN chat_sessions cs ON cs.id = i.aurora_chat_session_id::text
        WHERE i.org_id = %s
          AND cs.id IS NOT NULL
          AND i.created_at >= NOW() - %s::interval
    """

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[TOOL_STATS]")

                cur.execute(query, (org_id, pg_interval))
                cols = [d[0] for d in cur.description]
                tools = [dict(zip(cols, row)) for row in cur.fetchall()]

                cur.execute(rca_stats_query, (org_id, pg_interval))
                rca_row = cur.fetchone()

        for t in tools:
            for k, v in t.items():
                if v is None:
                    pass
                elif hasattr(v, "__float__") and not isinstance(v, (int, float)):
                    t[k] = float(v)

        rca_summary = {}
        if rca_row:
            rca_cols = ["total_rcas", "successful_rcas", "failed_rcas",
                        "avg_tool_calls_per_rca", "avg_thoughts_per_rca",
                        "avg_rca_duration_seconds", "avg_tokens_per_rca", "avg_cost_per_rca",
                        "avg_tool_duration_ms"]
            rca_summary = dict(zip(rca_cols, rca_row))
            for k, v in rca_summary.items():
                if v is None:
                    pass
                elif hasattr(v, "__float__") and not isinstance(v, (int, float)):
                    rca_summary[k] = float(v)

        return jsonify({"tools": tools, "rca_summary": rca_summary}), 200
    except Exception:
        logger.exception("tool_stats failed")
        return jsonify({"error": "Failed to fetch tool stats"}), 500


@waterfall_bp.route("/api/monitor/agent-sessions", methods=["GET"])
@require_permission("incidents", "read")
def agent_sessions(user_id):
    """Recent agent session telemetry — model, timing, context, retries, outcome."""
    org_id = get_org_id_from_request()
    time_range = request.args.get("time_range", "30d")
    interval_map = {"1d": "1 day", "7d": "7 days", "30d": "30 days", "90d": "90 days"}
    pg_interval = interval_map.get(time_range, "30 days")

    query = """
        SELECT
            a.id,
            a.session_id,
            a.incident_id,
            a.model_name,
            a.detected_provider,
            a.provider_mode,
            a.use_direct_sdk,
            a.temperature,
            a.mode,
            a.is_background,
            a.recursion_limit,
            a.context_messages_loaded,
            a.context_load_ms,
            a.rca_compression_applied,
            a.rca_compression_before,
            a.rca_compression_after,
            a.preflight_compression_applied,
            a.middleware_trim_applied,
            a.middleware_tokens_before,
            a.middleware_tokens_after,
            a.time_to_first_token_ms,
            a.total_events,
            a.total_tokens_streamed,
            a.model_turns,
            a.tool_calls_count,
            a.tool_errors_count,
            a.retry_attempts,
            a.last_retry_error,
            a.total_input_tokens,
            a.total_output_tokens,
            a.total_llm_calls,
            a.total_cost,
            a.status,
            a.error_message,
            a.placeholder_warning,
            a.duration_ms,
            a.started_at,
            a.completed_at,
            i.alert_service,
            i.alert_title,
            i.severity
        FROM agent_sessions a
        LEFT JOIN incidents i ON i.id = a.incident_id
        WHERE a.org_id = %s
          AND a.started_at >= NOW() - %s::interval
        ORDER BY a.started_at DESC
        LIMIT 50
    """

    summary_query = """
        SELECT
            COUNT(*) AS total_sessions,
            COUNT(*) FILTER (WHERE status = 'completed') AS completed,
            COUNT(*) FILTER (WHERE status = 'error') AS errored,
            ROUND(AVG(duration_ms) FILTER (WHERE duration_ms IS NOT NULL)) AS avg_duration_ms,
            ROUND(AVG(time_to_first_token_ms) FILTER (WHERE time_to_first_token_ms IS NOT NULL)) AS avg_ttft_ms,
            ROUND(AVG(total_tokens_streamed) FILTER (WHERE total_tokens_streamed IS NOT NULL)) AS avg_tokens_streamed,
            ROUND(AVG(model_turns) FILTER (WHERE model_turns IS NOT NULL), 1) AS avg_model_turns,
            ROUND(AVG(tool_calls_count) FILTER (WHERE tool_calls_count IS NOT NULL), 1) AS avg_tool_calls,
            ROUND(AVG(total_cost::numeric) FILTER (WHERE total_cost IS NOT NULL), 4) AS avg_cost,
            SUM(retry_attempts) AS total_retries,
            COUNT(*) FILTER (WHERE rca_compression_applied) AS rca_compressions,
            COUNT(*) FILTER (WHERE preflight_compression_applied) AS preflight_compressions,
            COUNT(*) FILTER (WHERE middleware_trim_applied) AS middleware_trims
        FROM agent_sessions
        WHERE org_id = %s
          AND started_at >= NOW() - %s::interval
    """

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[AGENT_SESSIONS]")

                cur.execute(query, (org_id, pg_interval))
                cols = [d[0] for d in cur.description]
                sessions = [dict(zip(cols, row)) for row in cur.fetchall()]

                cur.execute(summary_query, (org_id, pg_interval))
                summary_row = cur.fetchone()

        def _serialize(obj):
            for k, v in obj.items():
                if hasattr(v, "isoformat"):
                    obj[k] = v.isoformat()
                elif isinstance(v, type(None)):
                    pass
                elif hasattr(v, "__float__") and not isinstance(v, (int, float)):
                    obj[k] = float(v)

        for s in sessions:
            _serialize(s)

        summary = {}
        if summary_row:
            summary_cols = [
                "total_sessions", "completed", "errored", "avg_duration_ms",
                "avg_ttft_ms", "avg_tokens_streamed", "avg_model_turns",
                "avg_tool_calls", "avg_cost", "total_retries",
                "rca_compressions", "preflight_compressions", "middleware_trims",
            ]
            summary = dict(zip(summary_cols, summary_row))
            _serialize(summary)

        return jsonify({"sessions": sessions, "summary": summary}), 200
    except Exception:
        logger.exception("agent_sessions failed")
        return jsonify({"error": "Failed to fetch agent sessions"}), 500
