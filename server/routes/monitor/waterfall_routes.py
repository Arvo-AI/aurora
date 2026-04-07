"""Waterfall routes -- per-incident execution timeline + aggregated tool/RCA stats.

Pulls from three data sources:
  - incident_citations: tool calls with outputs (the primary source of "what the agent did")
  - execution_steps: real-time timing data (duration_ms, status) captured during tool execution
  - incident_thoughts: agent reasoning steps
  - llm_usage_tracking: LLM calls with token counts, costs, response times
"""
import json
import logging
import time
from flask import Blueprint, request, jsonify, Response
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request, set_rls_context
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

waterfall_bp = Blueprint("monitor_waterfall", __name__)


@waterfall_bp.route("/api/monitor/incidents/<incident_id>/timeline", methods=["GET"])
@require_permission("incidents", "read")
def incident_timeline(user_id, incident_id):
    """Execution timeline for one incident.

    Returns:
      steps    – tool call execution from execution_steps (authoritative)
      llm_calls – LLM invocations with token counts and costs
      thoughts  – agent reasoning (separate, not interleaved)
      summary   – aggregate counters
    """
    org_id = get_org_id_from_request()

    steps_query = """
        SELECT es.id, es.tool_name, es.tool_input, LEFT(es.tool_output, 4000) AS tool_output,
               es.step_index, es.status, es.error_message, es.duration_ms,
               es.started_at, es.completed_at
        FROM execution_steps es
        WHERE es.incident_id = %s
        ORDER BY es.started_at ASC, es.step_index ASC
    """

    llm_query = """
        SELECT lu.id, lu.model_name, lu.request_type,
               lu.total_tokens, lu.input_tokens, lu.output_tokens,
               lu.total_cost_with_surcharge AS cost,
               lu.response_time_ms, lu.error_message, lu.timestamp
        FROM llm_usage_tracking lu
        WHERE lu.session_id = (
            SELECT aurora_chat_session_id::text FROM incidents WHERE id = %s
        )
        ORDER BY lu.timestamp ASC
    """

    thoughts_query = """
        SELECT it.id, it.thought_type, LEFT(it.content, 2000) AS content,
               COALESCE(it.timestamp, it.created_at) AS event_time
        FROM incident_thoughts it
        WHERE it.incident_id = %s
        ORDER BY COALESCE(it.timestamp, it.created_at) ASC
    """

    summary_query = """
        SELECT
            (SELECT COUNT(*) FROM execution_steps es WHERE es.incident_id = i.id) AS total_tool_calls,
            (SELECT COUNT(*) FROM incident_thoughts it WHERE it.incident_id = i.id) AS total_thoughts,
            (SELECT COUNT(*) FROM llm_usage_tracking lu
             WHERE lu.session_id = i.aurora_chat_session_id::text) AS total_llm_calls,
            (SELECT SUM(lu.total_tokens) FROM llm_usage_tracking lu
             WHERE lu.session_id = i.aurora_chat_session_id::text) AS total_tokens,
            (SELECT SUM(lu.total_cost_with_surcharge) FROM llm_usage_tracking lu
             WHERE lu.session_id = i.aurora_chat_session_id::text) AS total_cost,
            (SELECT COUNT(*) FROM execution_steps es
             WHERE es.incident_id = i.id AND es.status = 'error') AS tool_errors,
            (SELECT ROUND(AVG(es.duration_ms)) FROM execution_steps es
             WHERE es.incident_id = i.id AND es.duration_ms IS NOT NULL) AS avg_tool_duration_ms,
            i.aurora_status,
            i.created_at AS started_at,
            i.analyzed_at AS completed_at,
            EXTRACT(EPOCH FROM (i.analyzed_at - i.created_at))::int AS duration_seconds
        FROM incidents i
        WHERE i.id = %s AND i.org_id = %s
    """

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[TIMELINE]")

                cur.execute(steps_query, (incident_id,))
                steps = [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]

                cur.execute(llm_query, (incident_id,))
                llm_calls = [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]

                cur.execute(thoughts_query, (incident_id,))
                thoughts = [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]

                cur.execute(summary_query, (incident_id, org_id))
                summary_row = cur.fetchone()

        def _ser(obj):
            for k, v in obj.items():
                if hasattr(v, "isoformat"):
                    obj[k] = v.isoformat()
                elif v is None:
                    pass
                elif hasattr(v, "__float__") and not isinstance(v, (int, float)):
                    obj[k] = float(v)
            return obj

        for s in steps:
            _ser(s)
        for l in llm_calls:
            _ser(l)
        for t in thoughts:
            _ser(t)

        summary = {}
        if summary_row:
            summary_cols = ["total_tool_calls", "total_thoughts", "total_llm_calls",
                            "total_tokens", "total_cost",
                            "tool_errors", "avg_tool_duration_ms",
                            "aurora_status", "started_at", "completed_at", "duration_seconds"]
            summary = _ser(dict(zip(summary_cols, summary_row)))

        return jsonify({
            "incident_id": incident_id,
            "summary": summary,
            "steps": steps,
            "llm_calls": llm_calls,
            "thoughts": thoughts,
        }), 200
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
            CASE
              WHEN es.tool_name = 'cloud_exec' AND es.tool_input IS NOT NULL
              THEN COALESCE(es.tool_input ->> 'provider', 'cloud') || '_exec'
              ELSE es.tool_name
            END AS tool_name,
            COUNT(*) AS call_count,
            COUNT(DISTINCT es.incident_id) AS incident_count,
            ROUND(AVG(es.duration_ms) FILTER (WHERE es.duration_ms IS NOT NULL)) AS avg_duration_ms,
            ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY es.duration_ms)
                  FILTER (WHERE es.duration_ms IS NOT NULL)) AS p95_duration_ms,
            COUNT(*) FILTER (WHERE es.status = 'error') AS error_count,
            ROUND(100.0 * COUNT(*) FILTER (WHERE es.status = 'success')
                  / GREATEST(COUNT(*) FILTER (WHERE es.status IN ('success', 'error')), 1), 1) AS success_rate
        FROM execution_steps es
        JOIN incidents i ON i.id = es.incident_id
        WHERE i.org_id = %s
          AND es.started_at >= NOW() - %s::interval
          AND es.tool_name IS NOT NULL
        GROUP BY 1
        ORDER BY call_count DESC
    """

    rca_stats_query = """
        SELECT
            COUNT(*) AS total_rcas,
            COUNT(*) FILTER (WHERE i.aurora_status IN ('complete', 'completed', 'resolved', 'analyzed')) AS successful_rcas,
            COUNT(*) FILTER (WHERE i.aurora_status = 'error') AS failed_rcas,
            ROUND(AVG(es_agg.cnt), 1) AS avg_tool_calls_per_rca,
            ROUND(AVG(th_agg.cnt), 1) AS avg_thoughts_per_rca,
            ROUND(AVG(EXTRACT(EPOCH FROM (i.analyzed_at - i.created_at)))
                FILTER (WHERE i.analyzed_at IS NOT NULL), 0) AS avg_rca_duration_seconds,
            ROUND(AVG(lu_agg.total_tokens), 0) AS avg_tokens_per_rca,
            ROUND(AVG(lu_agg.total_cost)::numeric, 4) AS avg_cost_per_rca,
            ROUND(AVG(es_agg.avg_dur), 0) AS avg_tool_duration_ms
        FROM incidents i
        JOIN chat_sessions cs ON cs.id = i.aurora_chat_session_id::text
        LEFT JOIN LATERAL (
            SELECT COUNT(*) AS cnt, AVG(es.duration_ms) FILTER (WHERE es.duration_ms IS NOT NULL) AS avg_dur
            FROM execution_steps es WHERE es.incident_id = i.id
        ) es_agg ON true
        LEFT JOIN LATERAL (
            SELECT COUNT(*) AS cnt
            FROM incident_thoughts it WHERE it.incident_id = i.id
        ) th_agg ON true
        LEFT JOIN LATERAL (
            SELECT SUM(lu.total_tokens) AS total_tokens,
                   SUM(lu.total_cost_with_surcharge) AS total_cost
            FROM llm_usage_tracking lu
            WHERE lu.session_id = i.aurora_chat_session_id::text
        ) lu_agg ON true
        WHERE i.org_id = %s
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


# ---------- SSE live stream for an incident's execution timeline ----------

def _serialize_row(row_dict):
    """JSON-safe serialisation for a DB row dict."""
    for k, v in row_dict.items():
        if hasattr(v, "isoformat"):
            row_dict[k] = v.isoformat()
        elif isinstance(v, type(None)):
            pass
        elif hasattr(v, "__float__") and not isinstance(v, (int, float)):
            row_dict[k] = float(v)
    return row_dict


@waterfall_bp.route("/api/monitor/incidents/<incident_id>/stream", methods=["GET"])
@require_permission("incidents", "read")
def incident_stream(user_id, incident_id):
    """SSE stream that pushes new execution_steps, thoughts, and llm_calls
    as they happen for a running incident.

    The client receives events:
      event: step       – new or updated execution_step row
      event: thought    – new incident_thought
      event: llm_call   – new llm_usage_tracking row
      event: done       – incident has finished (status is terminal)
    """
    org_id = get_org_id_from_request()

    # Preflight: verify the incident exists and belongs to this org before starting the SSE loop
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[STREAM_PREFLIGHT]")
                cur.execute(
                    "SELECT 1 FROM incidents WHERE id = %s AND org_id = %s",
                    (incident_id, org_id),
                )
                if cur.fetchone() is None:
                    return jsonify({"error": "Incident not found"}), 404
    except Exception:
        logger.exception("incident_stream preflight failed")
        return jsonify({"error": "Failed to open stream"}), 500

    POLL_INTERVAL = 1.5          # seconds between DB polls
    MAX_IDLE = 600               # stop after 10 min with no terminal state
    TERMINAL_STATUSES = {"complete", "completed", "resolved", "analyzed", "error"}

    def generate():
        last_step_ts = None
        seen_step_ids = set()
        seen_thought_ids = set()
        seen_llm_ids = set()
        elapsed = 0

        while elapsed < MAX_IDLE:
            try:
                with db_pool.get_admin_connection() as conn:
                    with conn.cursor() as cur:
                        set_rls_context(cur, conn, user_id, log_prefix="[STREAM]")

                        # 1. Execution steps — incremental: fetch new or changed rows
                        if last_step_ts:
                            cur.execute("""
                                SELECT id, incident_id, tool_name, tool_input, tool_output,
                                       step_index, status, error_message, duration_ms,
                                       started_at, completed_at
                                FROM execution_steps
                                WHERE incident_id = %s
                                  AND (started_at >= %s
                                       OR completed_at >= %s
                                       OR status = 'running')
                                ORDER BY started_at ASC
                            """, (incident_id, last_step_ts, last_step_ts))
                        else:
                            cur.execute("""
                                SELECT id, incident_id, tool_name, tool_input, tool_output,
                                       step_index, status, error_message, duration_ms,
                                       started_at, completed_at
                                FROM execution_steps
                                WHERE incident_id = %s
                                ORDER BY started_at ASC
                            """, (incident_id,))
                        cols = [d[0] for d in cur.description]
                        for row in cur.fetchall():
                            d = dict(zip(cols, row))
                            step_key = (d["id"], d.get("status"), d.get("duration_ms"))
                            if step_key not in seen_step_ids:
                                seen_step_ids.add(step_key)
                                if d.get("started_at") and hasattr(d["started_at"], "isoformat"):
                                    last_step_ts = d["started_at"]
                                _serialize_row(d)
                                yield f"event: step\ndata: {json.dumps(d)}\n\n"

                        # 2. Thoughts
                        cur.execute("""
                            SELECT id, thought_type, content, created_at,
                                   COALESCE(timestamp, created_at) AS event_time
                            FROM incident_thoughts
                            WHERE incident_id = %s
                            ORDER BY COALESCE(timestamp, created_at) ASC
                        """, (incident_id,))
                        cols = [d[0] for d in cur.description]
                        for row in cur.fetchall():
                            d = dict(zip(cols, row))
                            if d["id"] not in seen_thought_ids:
                                seen_thought_ids.add(d["id"])
                                _serialize_row(d)
                                yield f"event: thought\ndata: {json.dumps(d)}\n\n"

                        # 3. LLM calls
                        cur.execute("""
                            SELECT lu.id, lu.model_name, lu.request_type,
                                   lu.total_tokens, lu.total_cost_with_surcharge AS cost,
                                   lu.response_time_ms, lu.error_message, lu.timestamp
                            FROM llm_usage_tracking lu
                            WHERE lu.session_id = (
                                SELECT aurora_chat_session_id::text
                                FROM incidents WHERE id = %s
                            )
                            ORDER BY lu.timestamp ASC
                        """, (incident_id,))
                        cols = [d[0] for d in cur.description]
                        for row in cur.fetchall():
                            d = dict(zip(cols, row))
                            if d["id"] not in seen_llm_ids:
                                seen_llm_ids.add(d["id"])
                                _serialize_row(d)
                                yield f"event: llm_call\ndata: {json.dumps(d)}\n\n"

                        # 4. Check incident terminal status
                        cur.execute("""
                            SELECT aurora_status FROM incidents WHERE id = %s
                        """, (incident_id,))
                        status_row = cur.fetchone()
                        if status_row and status_row[0] in TERMINAL_STATUSES:
                            yield f"event: done\ndata: {json.dumps({'status': status_row[0]})}\n\n"
                            return

            except Exception:
                logger.exception("SSE stream poll error")
                yield f"event: error\ndata: {json.dumps({'error': 'poll_error'})}\n\n"

            time.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL

        yield f"event: done\ndata: {json.dumps({'status': 'timeout', 'message': 'Stream max idle reached'})}\n\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
