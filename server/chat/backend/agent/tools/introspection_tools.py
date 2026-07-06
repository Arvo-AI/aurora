"""
Introspection Tools — self-audit capabilities for the internal agent.

These give the internal agent (web chat, Slack, background RCA, Actions) the
same incident / infra / actions / metrics visibility that MCP clients have.
Everything reads Postgres or the graph DB directly — no Flask HTTP round-trip.

Each tool is a thin query wrapped by ``@introspection_tool``, which removes the
boilerplate every tool would otherwise repeat: requiring a user context,
JSON-serializing the result, and turning failures into a clean ``{"error": …}``
payload. Shared DB access goes through the ``_cursor`` helper, which scopes RLS
to the caller's org and hands back the resolved ``org_id`` in one step.
"""

import functools
import json
import logging
import re
from contextlib import contextmanager
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field

from chat.backend.agent.utils.tool_call_history import (
    MAX_HISTORY_ENTRIES,
    OUTPUT_EXCERPT_MAX_CHARS,
    TERMINAL_STATUSES,
    history_from_step_rows,
)
from utils.metrics_periods import period_to_interval
from utils.query_helpers import clamp, duration_ms, fetch_dicts, iso_utc
from utils.validation import is_valid_uuid
from utils.db.connection_pool import db_pool
from utils.auth.stateless_auth import set_rls_context

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[IntrospectionTools]"


# ---------------------------------------------------------------------------
# Shared infrastructure
# ---------------------------------------------------------------------------

class IntrospectionError(Exception):
    """Raised inside a tool to return a clean, user-facing error message."""


def introspection_tool(fn: Callable[..., Any]) -> Callable[..., str]:
    """Wrap a tool so it only has to contain its query logic.

    Responsibilities lifted out of every tool:
      * require an agent ``user_id`` (injected by ``with_user_context``),
      * serialize the returned dict/list to JSON,
      * surface ``IntrospectionError`` as ``{"error": message}``,
      * log and mask any unexpected exception.

    Wrapped tools take ``user_id`` as a keyword arg and return a plain dict.
    """
    @functools.wraps(fn)
    def wrapper(*args, user_id: Optional[str] = None, **kwargs) -> str:
        if not user_id:
            return json.dumps({"error": "No user context available."})
        try:
            return json.dumps(fn(*args, user_id=user_id, **kwargs), default=str)
        except IntrospectionError as exc:
            return json.dumps({"error": str(exc)})
        except Exception:
            logger.exception("%s %s failed", _LOG_PREFIX, fn.__name__)
            return json.dumps({"error": f"Failed to run {fn.__name__}."})

    return wrapper


@contextmanager
def _cursor(user_id: str):
    """Yield ``(cursor, org_id)`` on a pooled connection with RLS scoped to the org."""
    with db_pool.get_connection() as conn:
        with conn.cursor() as cur:
            org_id = set_rls_context(cur, conn, user_id, log_prefix=_LOG_PREFIX)
            if not org_id:
                raise IntrospectionError("No organization context.")
            yield cur, org_id


def _require_uuid(value: Optional[str], field: str = "id") -> str:
    """Validate that ``value`` is a UUID, or fail with a tool-facing error.

    Surfaces as an ``IntrospectionError`` (which the decorator turns into a clean
    payload) rather than the bare bool the shared predicate returns.
    """
    if not is_valid_uuid(value):
        raise IntrospectionError(f"A valid {field} UUID is required.")
    return value


# ---------------------------------------------------------------------------
# Incidents
# ---------------------------------------------------------------------------

class ListIncidentsArgs(BaseModel):
    status: Optional[str] = Field(
        default=None,
        description="Filter by incident status: investigating, analyzed, merged, or resolved.",
    )
    limit: int = Field(default=20, description="Max incidents to return (1–100, default 20).")
    offset: int = Field(default=0, description="Paging offset (>= 0, default 0).")


@introspection_tool
def list_incidents(status=None, limit=20, offset=0, *, user_id, **_) -> dict:
    """List Aurora incidents with optional status filter and pagination."""
    limit, offset = clamp(limit, 1, 100), max(0, int(offset))

    with _cursor(user_id) as (cur, org_id):
        where = "i.org_id = %s"
        params = [org_id]
        if status:
            where += " AND i.status = %s"
            params.append(status)
        else:
            # Hide merged by default unless caller explicitly filters for them
            where += " AND i.status != 'merged'"

        cur.execute(f"SELECT COUNT(*) FROM incidents i WHERE {where}", tuple(params))
        total = cur.fetchone()[0]
        cur.execute(
            f"""SELECT i.id, i.status, i.severity, i.alert_title, i.alert_service,
                       i.alert_environment, i.aurora_status, i.aurora_summary,
                       i.started_at, i.analyzed_at, i.resolved_at, i.source_type
                FROM incidents i WHERE {where}
                ORDER BY i.started_at DESC LIMIT %s OFFSET %s""",
            tuple(params + [limit, offset]),
        )
        rows = cur.fetchall()

    incidents = [
        {
            "id": str(r[0]), "status": r[1], "severity": r[2], "title": r[3],
            "service": r[4], "environment": r[5], "auroraStatus": r[6], "summary": r[7],
            "startedAt": iso_utc(r[8]), "analyzedAt": iso_utc(r[9]), "resolvedAt": iso_utc(r[10]),
            "sourceType": r[11],
        }
        for r in rows
    ]
    return {"incidents": incidents, "total": total}


class GetIncidentArgs(BaseModel):
    incident_id: str = Field(description="The UUID of the incident to retrieve.")


@introspection_tool
def get_incident(incident_id, *, user_id, **_) -> dict:
    """Get full incident details: summary, suggestions, and correlated alerts."""
    _require_uuid(incident_id, "incident_id")

    with _cursor(user_id) as (cur, org_id):
        cur.execute(
            """SELECT id, status, severity, alert_title, alert_service, alert_environment,
                      aurora_status, aurora_summary, started_at, analyzed_at, resolved_at,
                      source_type, affected_services, correlated_alert_count
               FROM incidents WHERE id = %s AND org_id = %s""",
            (incident_id, org_id),
        )
        row = cur.fetchone()
        if not row:
            raise IntrospectionError("Incident not found.")

        incident = {
            "id": str(row[0]), "status": row[1], "severity": row[2], "title": row[3],
            "service": row[4], "environment": row[5], "auroraStatus": row[6], "summary": row[7],
            "startedAt": iso_utc(row[8]), "analyzedAt": iso_utc(row[9]), "resolvedAt": iso_utc(row[10]),
            "sourceType": row[11], "affectedServices": row[12] or [],
            "correlatedAlertCount": row[13] or 0,
        }

        cur.execute(
            """SELECT id, title, description, type, risk, command, created_at
               FROM incident_suggestions WHERE incident_id = %s ORDER BY created_at ASC""",
            (incident_id,),
        )
        incident["suggestions"] = [
            {
                "id": str(s[0]), "title": s[1], "description": s[2],
                "type": s[3] or "diagnostic", "risk": s[4] or "safe",
                "command": s[5], "createdAt": iso_utc(s[6]),
            }
            for s in cur.fetchall()
        ]

        cur.execute(
            """SELECT id, source_type, alert_title, alert_service, alert_severity,
                      correlation_score, received_at
               FROM incident_alerts WHERE incident_id = %s ORDER BY received_at ASC""",
            (incident_id,),
        )
        incident["correlatedAlerts"] = [
            {
                "id": str(a[0]), "sourceType": a[1], "title": a[2], "service": a[3],
                "severity": a[4], "correlationScore": a[5], "receivedAt": iso_utc(a[6]),
            }
            for a in cur.fetchall()
        ]

    return {"incident": incident}


class IncidentListAlertsArgs(BaseModel):
    incident_id: str = Field(
        description="The UUID of the incident whose correlated alerts to list.",
    )


@introspection_tool
def incident_list_alerts(incident_id, *, user_id, **_) -> dict:
    """List the alerts correlated to an incident with correlation details."""
    _require_uuid(incident_id, "incident_id")

    with _cursor(user_id) as (cur, org_id):
        cur.execute(
            "SELECT 1 FROM incidents WHERE id = %s AND org_id = %s", (incident_id, org_id)
        )
        if not cur.fetchone():
            raise IntrospectionError("Incident not found.")

        cur.execute(
            """SELECT id, source_type, alert_title, alert_service, alert_severity,
                      correlation_strategy, correlation_score, correlation_details, received_at
               FROM incident_alerts WHERE incident_id = %s ORDER BY received_at ASC""",
            (incident_id,),
        )
        alerts = [
            {
                "id": str(r[0]), "sourceType": r[1], "title": r[2], "service": r[3],
                "severity": r[4], "correlationStrategy": r[5], "correlationScore": r[6],
                "correlationDetails": r[7] if isinstance(r[7], dict) else {},
                "receivedAt": iso_utc(r[8]),
            }
            for r in cur.fetchall()
        ]

    return {"alerts": alerts, "total": len(alerts)}


# ---------------------------------------------------------------------------
# Service dependency graph
# ---------------------------------------------------------------------------

class ListServicesArgs(BaseModel):
    resource_type: Optional[str] = Field(
        default=None, description="Filter by resource type (e.g. 'compute', 'database')."
    )
    provider: Optional[str] = Field(
        default=None, description="Filter by cloud provider (e.g. 'aws', 'gcp')."
    )


@introspection_tool
def list_services(resource_type=None, provider=None, *, user_id, **_) -> dict:
    """List services in the infrastructure dependency graph."""
    from services.graph.memgraph_client import get_memgraph_client

    services = get_memgraph_client().list_services(
        user_id, resource_type=resource_type, provider=provider
    )
    return {"services": services, "total": len(services)}


class ServiceImpactArgs(BaseModel):
    name: str = Field(description="The service name exactly as it appears in the dependency graph.")


@introspection_tool
def service_impact(name, *, user_id, **_) -> dict:
    """Get a service's blast radius — the downstream services that depend on it."""
    if not name or not name.strip():
        raise IntrospectionError("Service name is required.")
    from services.graph.memgraph_client import get_memgraph_client

    return get_memgraph_client().get_impact_radius(user_id, name.strip())


class GraphGetServiceArgs(BaseModel):
    name: str = Field(description="The service name exactly as it appears in the dependency graph.")


@introspection_tool
def graph_get_service(name, *, user_id, **_) -> dict:
    """Get a service with its direct upstream and downstream dependencies."""
    if not name or not name.strip():
        raise IntrospectionError("Service name is required.")
    from services.graph.memgraph_client import get_memgraph_client

    service = get_memgraph_client().get_service(user_id, name.strip())
    if not service:
        raise IntrospectionError(f"Service '{name}' not found in graph.")
    return service


# ---------------------------------------------------------------------------
# Actions (automations)
# ---------------------------------------------------------------------------

class ListActionsArgs(BaseModel):
    pass


@introspection_tool
def list_actions(*, user_id, **_) -> dict:
    """List the org's Aurora actions (automations) with run counts and last status."""
    with _cursor(user_id) as (cur, org_id):
        cur.execute(
            """SELECT a.id, a.name, a.description, a.trigger_type, a.mode, a.enabled,
                      a.created_at, a.updated_at, COUNT(r.id) AS run_count,
                      MAX(r.started_at) AS last_run_at,
                      (SELECT r2.status FROM action_runs r2 WHERE r2.action_id = a.id
                       ORDER BY r2.started_at DESC LIMIT 1) AS last_run_status
               FROM actions a
               LEFT JOIN action_runs r ON r.action_id = a.id
               WHERE a.org_id = %s
               GROUP BY a.id
               ORDER BY a.is_system DESC, a.created_at DESC""",
            (org_id,),
        )
        rows = fetch_dicts(cur)

    actions = [
        {
            "id": str(a["id"]), "name": a["name"], "description": a["description"],
            "triggerType": a["trigger_type"], "mode": a["mode"], "enabled": a["enabled"],
            "runCount": a["run_count"] or 0, "lastRunAt": iso_utc(a["last_run_at"]),
            "lastRunStatus": a["last_run_status"], "createdAt": iso_utc(a["created_at"]),
            "updatedAt": iso_utc(a["updated_at"]),
        }
        for a in rows
    ]
    return {"actions": actions, "total": len(actions)}


class ListActionRunsArgs(BaseModel):
    action_id: str = Field(description="The UUID of the action whose run history to list.")
    limit: int = Field(default=50, description="Max runs to return (1–200, default 50).")
    offset: int = Field(default=0, description="Paging offset (>= 0, default 0).")


@introspection_tool
def list_action_runs(action_id, limit=50, offset=0, *, user_id, **_) -> dict:
    """List an action's run history: status, timing, linked incident, and errors."""
    _require_uuid(action_id, "action_id")
    limit, offset = clamp(limit, 1, 200), max(0, int(offset))

    with _cursor(user_id) as (cur, org_id):
        # Verify the action belongs to this org before exposing run history
        cur.execute("SELECT 1 FROM actions WHERE id = %s AND org_id = %s", (action_id, org_id))
        if not cur.fetchone():
            raise IntrospectionError("Action not found.")

        cur.execute(
            """SELECT id, status, incident_id, chat_session_id, started_at, completed_at, error
               FROM action_runs WHERE action_id = %s
               ORDER BY started_at DESC LIMIT %s OFFSET %s""",
            (action_id, limit, offset),
        )
        rows = fetch_dicts(cur)

    runs = [
        {
            "id": str(r["id"]), "status": r["status"],
            "incidentId": str(r["incident_id"]) if r["incident_id"] else None,
            "chatSessionId": str(r["chat_session_id"]) if r["chat_session_id"] else None,
            "startedAt": iso_utc(r["started_at"]), "completedAt": iso_utc(r["completed_at"]),
            "durationMs": duration_ms(r["started_at"], r["completed_at"]), "error": r["error"],
        }
        for r in rows
    ]
    return {"runs": runs, "total": len(runs)}


class GetActionArgs(BaseModel):
    action_id: str = Field(description="The UUID of the action to retrieve.")


@introspection_tool
def get_action(action_id, *, user_id, **_) -> dict:
    """Get an action's full config plus its 20 most recent runs."""
    _require_uuid(action_id, "action_id")

    with _cursor(user_id) as (cur, org_id):
        cur.execute(
            """SELECT id, name, description, instructions, trigger_type, trigger_config,
                      mode, enabled, is_system, system_key, created_at, updated_at
               FROM actions WHERE id = %s AND org_id = %s""",
            (action_id, org_id),
        )
        cols = [d[0] for d in cur.description]
        row = cur.fetchone()
        if not row:
            raise IntrospectionError("Action not found.")
        action = dict(zip(cols, row))

        cur.execute(
            """SELECT id, status, incident_id, chat_session_id, trigger_context,
                      started_at, completed_at, error
               FROM action_runs WHERE action_id = %s ORDER BY started_at DESC LIMIT 20""",
            (action_id,),
        )
        runs_raw = fetch_dicts(cur)

    action["id"] = str(action["id"])
    action["created_at"] = iso_utc(action.get("created_at"))
    action["updated_at"] = iso_utc(action.get("updated_at"))

    runs = [
        {
            "id": str(r["id"]), "status": r["status"],
            "incident_id": str(r["incident_id"]) if r.get("incident_id") else None,
            "started_at": iso_utc(r.get("started_at")), "completed_at": iso_utc(r.get("completed_at")),
            "duration_ms": duration_ms(r.get("started_at"), r.get("completed_at")),
            "error": r.get("error"),
        }
        for r in runs_raw
    ]
    return {"action": action, "recent_runs": runs}


# ---------------------------------------------------------------------------
# DORA / SRE metrics
# ---------------------------------------------------------------------------

# Resolve/analysis latency in seconds, reused across the metrics queries.
_MTTR_EPOCH = "EXTRACT(EPOCH FROM (COALESCE(resolved_at, analyzed_at) - started_at))"


class GetMetricsSummaryArgs(BaseModel):
    period: str = Field(
        default="30d", description="Time period: 7d, 30d, 90d, 180d, or 365d (default 30d)."
    )


@introspection_tool
def get_metrics_summary(period="30d", *, user_id, **_) -> dict:
    """Dashboard overview: incident counts, average MTTR/MTTS, and top services."""
    interval = period_to_interval(period)

    with _cursor(user_id) as (cur, _org):
        cur.execute(
            """SELECT
                   COUNT(*) FILTER (WHERE started_at >= NOW() - %s::interval) AS total,
                   COUNT(*) FILTER (
                       WHERE status IN ('investigating', 'analyzed')
                         AND aurora_status NOT IN ('complete', 'resolved')
                   ) AS active,
                   COUNT(*) FILTER (
                       WHERE status = 'resolved' AND resolved_at >= NOW() - %s::interval
                   ) AS resolved
               FROM incidents""",
            (interval, interval),
        )
        total, active, resolved = cur.fetchone()

        cur.execute(
            f"""SELECT AVG({_MTTR_EPOCH}) FROM incidents
                WHERE resolved_at IS NOT NULL AND status = 'resolved'
                  AND resolved_at >= NOW() - %s::interval""",
            (interval,),
        )
        avg_mttr = cur.fetchone()[0]

        cur.execute(
            """SELECT AVG(EXTRACT(EPOCH FROM (analyzed_at - started_at))) FROM incidents
               WHERE analyzed_at IS NOT NULL AND analyzed_at >= NOW() - %s::interval""",
            (interval,),
        )
        avg_mtts = cur.fetchone()[0]

        cur.execute(
            """SELECT alert_service, COUNT(*) AS cnt FROM incidents
               WHERE started_at >= NOW() - %s::interval
                 AND alert_service IS NOT NULL AND status != 'merged'
               GROUP BY alert_service ORDER BY cnt DESC LIMIT 10""",
            (interval,),
        )
        top_services = [{"service": r[0], "count": r[1]} for r in cur.fetchall()]

    return {
        "period": period,
        "totalIncidents": total or 0,
        "activeIncidents": active or 0,
        "resolvedIncidents": resolved or 0,
        "avgMttrSeconds": round(avg_mttr, 1) if avg_mttr else None,
        "avgMttsSeconds": round(avg_mtts, 1) if avg_mtts else None,
        "topServices": top_services,
    }


class GetMttrArgs(BaseModel):
    period: str = Field(default="30d", description="Time period: 7d, 30d, 90d, 180d, or 365d.")
    severity: Optional[str] = Field(
        default=None, description="Filter by severity (e.g. critical, high, medium, low)."
    )
    service: Optional[str] = Field(default=None, description="Filter by service name.")


@introspection_tool
def get_mttr(period="30d", severity=None, service=None, *, user_id, **_) -> dict:
    """Mean Time to Resolve with p50/p95, broken down by severity and trended daily."""
    where = ["resolved_at IS NOT NULL", "status = 'resolved'",
             "resolved_at >= NOW() - %s::interval"]
    params = [period_to_interval(period)]
    if severity:
        where.append("severity = %s")
        params.append(severity)
    if service:
        where.append("alert_service = %s")
        params.append(service)
    where_sql = " AND ".join(where)

    with _cursor(user_id) as (cur, _org):
        cur.execute(
            f"""SELECT COALESCE(severity, 'unknown') AS sev, COUNT(*) AS count,
                       AVG({_MTTR_EPOCH}) AS avg_mttr,
                       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY {_MTTR_EPOCH}) AS p50,
                       PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY {_MTTR_EPOCH}) AS p95
                FROM incidents WHERE {where_sql}
                GROUP BY sev ORDER BY count DESC""",
            tuple(params),
        )
        by_severity = [
            {
                "severity": r[0], "count": r[1],
                "avgSeconds": round(r[2], 1) if r[2] else None,
                "p50Seconds": round(r[3], 1) if r[3] else None,
                "p95Seconds": round(r[4], 1) if r[4] else None,
            }
            for r in cur.fetchall()
        ]

        cur.execute(
            f"""SELECT date_trunc('day', COALESCE(resolved_at, analyzed_at))::date AS day,
                       AVG({_MTTR_EPOCH}) AS avg_mttr, COUNT(*) AS count
                FROM incidents WHERE {where_sql}
                GROUP BY day ORDER BY day ASC""",
            tuple(params),
        )
        trend = [
            {"date": str(r[0]), "avgSeconds": round(r[1], 1) if r[1] else None, "count": r[2]}
            for r in cur.fetchall()
        ]

    return {"bySeverity": by_severity, "trend": trend, "period": period}


class GetIncidentFrequencyArgs(BaseModel):
    period: str = Field(default="30d", description="Time period: 7d, 30d, 90d, 180d, or 365d.")
    group_by: str = Field(
        default="severity", description="Group results by: severity, service, or source_type."
    )


@introspection_tool
def get_incident_frequency(period="30d", group_by="severity", *, user_id, **_) -> dict:
    """Incident count over time, grouped by severity, service, or source type."""
    if group_by not in ("severity", "service", "source_type"):
        group_by = "severity"
    # Whitelisted above, so this column name is safe to interpolate.
    group_col = "alert_service" if group_by == "service" else group_by

    with _cursor(user_id) as (cur, _org):
        cur.execute(
            f"""SELECT date_trunc('day', started_at)::date AS day,
                       COALESCE({group_col}, 'unknown') AS group_value, COUNT(*) AS count
                FROM incidents
                WHERE started_at >= NOW() - %s::interval AND status != 'merged'
                GROUP BY day, group_value ORDER BY day ASC, count DESC""",
            (period_to_interval(period),),
        )
        data = [{"date": str(r[0]), "group": r[1], "count": r[2]} for r in cur.fetchall()]

    return {"data": data, "groupBy": group_by, "period": period}


class GetChangeFailureRateArgs(BaseModel):
    period: str = Field(default="30d", description="Time period: 7d, 30d, 90d, 180d, or 365d.")


@introspection_tool
def get_change_failure_rate(period="30d", *, user_id, **_) -> dict:
    """Percentage of deployments that caused an incident within a 4-hour window."""
    with _cursor(user_id) as (cur, _org):
        cur.execute(
            """WITH deploys AS (
                   SELECT id, service, received_at FROM jenkins_deployment_events
                   WHERE received_at >= NOW() - %s::interval
               ),
               deploy_failures AS (
                   SELECT DISTINCT d.id FROM deploys d
                   JOIN incidents i ON (
                       i.alert_service = d.service
                       AND i.started_at BETWEEN d.received_at
                           AND d.received_at + make_interval(hours => 4)
                       AND i.status != 'merged'
                   )
               )
               SELECT (SELECT COUNT(*) FROM deploys),
                      (SELECT COUNT(*) FROM deploy_failures)""",
            (period_to_interval(period),),
        )
        total, failures = cur.fetchone()

    total, failures = total or 0, failures or 0
    return {
        "period": period,
        "totalDeployments": total,
        "failureLinked": failures,
        "changeFailureRate": round(failures / total * 100, 2) if total else 0,
    }


# ---------------------------------------------------------------------------
# LLM usage / cost
# ---------------------------------------------------------------------------

class GetLlmUsageSummaryArgs(BaseModel):
    period: str = Field(default="30d", description="Time period: 7d, 30d, 90d, 180d, or 365d.")


@introspection_tool
def get_llm_usage_summary(period="30d", *, user_id, **_) -> dict:
    """Aggregate LLM token usage and estimated cost for the org."""
    with _cursor(user_id) as (cur, org_id):
        cur.execute(
            """SELECT COALESCE(SUM(estimated_cost), 0), COALESCE(SUM(total_tokens), 0),
                      COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0),
                      COUNT(*), COUNT(*) FILTER (WHERE error_message IS NOT NULL),
                      ROUND(AVG(response_time_ms) FILTER (WHERE response_time_ms IS NOT NULL)),
                      COUNT(DISTINCT model_name)
               FROM llm_usage_tracking
               WHERE org_id = %s AND timestamp >= NOW() - %s::interval""",
            (org_id, period_to_interval(period)),
        )
        cost, tokens, inp, outp, requests, errors, avg_ms, models = cur.fetchone()

    requests, errors = requests or 0, errors or 0
    return {
        "period": period,
        "totalCost": float(cost) if cost else 0.0,
        "totalTokens": tokens or 0,
        "inputTokens": inp or 0,
        "outputTokens": outp or 0,
        "totalRequests": requests,
        "errorCount": errors,
        "errorRate": round(errors / requests * 100, 1) if requests else 0,
        "avgResponseMs": int(avg_ms) if avg_ms else None,
        "modelsUsed": models or 0,
    }


# ---------------------------------------------------------------------------
# RCA findings
# ---------------------------------------------------------------------------

_AGENT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


class IncidentFindingsArgs(BaseModel):
    incident_id: str = Field(description="The UUID of the incident whose RCA findings to list.")


@introspection_tool
def incident_findings(incident_id, *, user_id, **_) -> dict:
    """List RCA sub-agent findings for an incident: role, status, tools, citations."""
    _require_uuid(incident_id, "incident_id")

    with _cursor(user_id) as (cur, _org):
        cur.execute(
            """SELECT agent_id, role_name, purpose, status, self_assessed_strength,
                      current_action, started_at, completed_at, tools_used, citations,
                      follow_ups_suggested, wave
               FROM rca_findings WHERE incident_id = %s ORDER BY started_at ASC""",
            (incident_id,),
        )
        rows = fetch_dicts(cur)

    findings = [
        {
            "agent_id": f["agent_id"], "role_name": f["role_name"], "purpose": f["purpose"],
            "status": f["status"], "wave": f.get("wave"),
            "self_assessed_strength": f.get("self_assessed_strength"),
            "current_action": f.get("current_action"),
            "started_at": iso_utc(f.get("started_at")), "completed_at": iso_utc(f.get("completed_at")),
            "tools_used": f.get("tools_used") or [], "citations": f.get("citations") or [],
            "follow_ups_suggested": f.get("follow_ups_suggested") or [],
        }
        for f in rows
    ]
    return {"findings": findings, "count": len(findings)}


class IncidentFindingDetailArgs(BaseModel):
    incident_id: str = Field(description="The UUID of the incident.")
    agent_id: str = Field(
        description="The sub-agent ID (alphanumeric/dash/underscore, max 64 chars)."
    )


@introspection_tool
def incident_finding_detail(incident_id, agent_id, *, user_id, **_) -> dict:
    """Get one sub-agent's full finding body plus its step-by-step tool history."""
    _require_uuid(incident_id, "incident_id")
    if not _AGENT_ID_RE.match(agent_id or ""):
        raise IntrospectionError("Invalid agent_id format.")

    with _cursor(user_id) as (cur, _org):
        cur.execute(
            "SELECT storage_uri, status, tool_call_history, user_id "
            "FROM rca_findings WHERE incident_id = %s AND agent_id = %s",
            (incident_id, agent_id),
        )
        row = cur.fetchone()
        if not row:
            raise IntrospectionError("Finding not found.")
        storage_uri, status, archived_history, originator_id = row

        # Prefer live steps; sub-agents in flight have no archived blob yet.
        # Escape LIKE metacharacters — agent IDs may contain underscores
        escaped_agent_id = agent_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        cur.execute(
            """SELECT tool_name, tool_input, LEFT(tool_output, %s) AS tool_output,
                      status, started_at, completed_at
               FROM execution_steps
               WHERE incident_id = %s AND session_id LIKE %s AND tool_name <> 'write_findings'
               ORDER BY step_index ASC LIMIT %s""",
            (OUTPUT_EXCERPT_MAX_CHARS + 1, incident_id, f"%::{escaped_agent_id}", MAX_HISTORY_ENTRIES),
        )
        step_rows = cur.fetchall()

    history = history_from_step_rows(step_rows)
    # Fall back to the archived JSONB once terminal (live steps may be pruned).
    if not history and status in TERMINAL_STATUSES:
        history = archived_history or []

    return {
        "agent_id": agent_id,
        "status": status,
        "body": _load_finding_body(storage_uri, originator_id or user_id, agent_id),
        "tool_call_history": history,
    }


def _load_finding_body(storage_uri, storage_user_id, agent_id) -> Optional[str]:
    """Download a finding's markdown body from object storage, if it exists."""
    if not storage_uri:
        return None
    try:
        from utils.storage.storage import get_storage_manager

        data = get_storage_manager(storage_user_id).download_bytes(storage_uri, storage_user_id)
        return data.decode("utf-8") if isinstance(data, bytes) else str(data)
    except Exception:
        logger.warning("%s Could not fetch finding body for agent=%s", _LOG_PREFIX, agent_id)
        return None


# ---------------------------------------------------------------------------
# Postmortems
# ---------------------------------------------------------------------------

class PostmortemListArgs(BaseModel):
    limit: int = Field(default=50, description="Max postmortems to return (1–100, default 50).")
    offset: int = Field(default=0, description="Paging offset (>= 0, default 0).")


@introspection_tool
def postmortem_list(limit=50, offset=0, *, user_id, **_) -> dict:
    """List all postmortems for the org with incident titles and export URLs."""
    limit, offset = clamp(limit, 1, 100), max(0, int(offset))

    with _cursor(user_id) as (cur, org_id):
        cur.execute(
            """SELECT p.id, p.incident_id, p.generated_at, p.updated_at, i.alert_title,
                      p.confluence_page_url, p.jira_issue_url, p.notion_page_url
               FROM postmortems p
               LEFT JOIN incidents i ON p.incident_id = i.id
               WHERE p.org_id = %s
               ORDER BY p.generated_at DESC LIMIT %s OFFSET %s""",
            (org_id, limit, offset),
        )
        rows = cur.fetchall()

    postmortems = [
        {
            "id": str(r[0]), "incident_id": str(r[1]), "incident_title": r[4],
            "generated_at": iso_utc(r[2]), "updated_at": iso_utc(r[3]),
            "confluence_url": r[5], "jira_url": r[6], "notion_url": r[7],
        }
        for r in rows
    ]
    return {"postmortems": postmortems, "count": len(postmortems)}


# ---------------------------------------------------------------------------
# Knowledge base memory
# ---------------------------------------------------------------------------

class KbGetMemoryArgs(BaseModel):
    pass


@introspection_tool
def kb_get_memory(*, user_id, **_) -> dict:
    """Read the org's persistent knowledge base memory."""
    with _cursor(user_id) as (cur, org_id):
        cur.execute(
            """SELECT content, updated_at FROM knowledge_base_memory
               WHERE org_id = %s ORDER BY updated_at DESC LIMIT 1""",
            (org_id,),
        )
        row = cur.fetchone()

    if row and row[0]:
        return {"content": row[0], "updated_at": iso_utc(row[1])}
    return {"content": "", "updated_at": None}


# ---------------------------------------------------------------------------
# Grafana alerts (webhook-ingested)
# ---------------------------------------------------------------------------

class GrafanaListAlertsArgs(BaseModel):
    state: Optional[str] = Field(
        default=None, description="Filter by alert state (e.g. 'alerting', 'ok', 'pending')."
    )
    limit: int = Field(default=50, description="Max alerts to return (1–100, default 50).")


@introspection_tool
def grafana_list_alerts(state=None, limit=50, *, user_id, **_) -> dict:
    """List Grafana alerts ingested via webhook, optionally filtered by state."""
    limit = clamp(limit, 1, 100)

    with _cursor(user_id) as (cur, org_id):
        where = "org_id = %s"
        params = [org_id]
        if state:
            where += " AND alert_state = %s"
            params.append(state)

        cur.execute(
            f"""SELECT id, alert_uid, alert_title, alert_state, rule_name,
                       rule_url, dashboard_url, received_at
                FROM grafana_alerts WHERE {where}
                ORDER BY received_at DESC LIMIT %s""",
            tuple(params + [limit]),
        )
        rows = cur.fetchall()

    alerts = [
        {
            "id": r[0], "alert_uid": r[1], "title": r[2], "state": r[3],
            "rule_name": r[4], "rule_url": r[5], "dashboard_url": r[6],
            "received_at": iso_utc(r[7]),
        }
        for r in rows
    ]
    return {"alerts": alerts, "count": len(alerts)}
