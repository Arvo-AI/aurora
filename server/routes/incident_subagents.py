"""Read-only API routes for per-incident sub-agent run audit data."""

import logging

from flask import Blueprint, jsonify, request

from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import set_rls_context
from chat.backend.agent.orchestrator.findings_reader import (
    FindingsValidationError,
    read_findings,
)

logger = logging.getLogger(__name__)

incident_subagents_bp = Blueprint("incident_subagents", __name__)

MAX_TRANSCRIPT_EVENTS = 5000

RUN_COLUMNS = (
    "agent_id, parent_agent_id, role, delegate_level, purpose, ui_label, "
    "model_used, status, self_assessed_strength, started_at, ended_at, "
    "findings_artifact_ref, error, suggested_skill_focus, session_id"
)


def _row_to_run(row) -> dict:
    return {
        "agent_id": row[0],
        "parent_agent_id": row[1],
        "role": row[2],
        "delegate_level": row[3],
        "purpose": row[4],
        "ui_label": row[5],
        "model_used": row[6],
        "status": row[7],
        "self_assessed_strength": row[8],
        "started_at": row[9].isoformat() if row[9] else None,
        "ended_at": row[10].isoformat() if row[10] else None,
        "findings_artifact_ref": row[11],
        "error": row[12],
        "suggested_skill_focus": list(row[13]) if row[13] else [],
    }


def _incident_exists(cur, incident_id: str) -> bool:
    cur.execute("SELECT 1 FROM incidents WHERE id = %s", (incident_id,))
    return cur.fetchone() is not None


@incident_subagents_bp.route("/api/incidents/<incident_id>/subagents", methods=["GET"])
@require_permission("incidents", "read")
def list_runs(user_id, incident_id):
    from utils.db.connection_pool import db_pool
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[IncidentSubagents:list]")
            if not _incident_exists(cur, incident_id):
                return jsonify({"error": "Incident not found"}), 404
            cur.execute(
                f"SELECT {RUN_COLUMNS} FROM incident_subagent_runs "
                "WHERE incident_id = %s "
                "ORDER BY started_at ASC NULLS LAST, delegate_level ASC",
                (incident_id,),
            )
            rows = cur.fetchall()

    runs = [_row_to_run(r) for r in rows]
    return jsonify({"runs": runs})


@incident_subagents_bp.route(
    "/api/incidents/<incident_id>/subagents/<agent_id>", methods=["GET"]
)
@require_permission("incidents", "read")
def get_run(user_id, incident_id, agent_id):
    from utils.db.connection_pool import db_pool
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[IncidentSubagents:get]")
            if not _incident_exists(cur, incident_id):
                return jsonify({"error": "Incident not found"}), 404
            cur.execute(
                f"SELECT {RUN_COLUMNS} FROM incident_subagent_runs "
                "WHERE incident_id = %s AND agent_id = %s",
                (incident_id, agent_id),
            )
            row = cur.fetchone()

    if not row:
        return jsonify({"error": "Sub-agent run not found"}), 404

    run = _row_to_run(row)
    artifact_ref = run.get("findings_artifact_ref")

    findings_markdown = None
    findings_frontmatter = None
    findings_sections = None

    if artifact_ref:
        try:
            parsed = read_findings(artifact_ref)
            findings_frontmatter = parsed.get("frontmatter")
            findings_sections = parsed.get("sections")
            findings_markdown = parsed.get("body")
        except FindingsValidationError as e:
            logger.warning("[IncidentSubagents:get] malformed findings for %s: %s", agent_id, e)
            try:
                from utils.storage.storage import get_storage_manager
                raw = get_storage_manager().download_bytes(artifact_ref)
                findings_markdown = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
            except Exception as fetch_err:
                logger.warning("[IncidentSubagents:get] raw fetch failed for %s: %s", agent_id, fetch_err)
        except Exception as e:
            logger.warning("[IncidentSubagents:get] read_findings failed for %s: %s", agent_id, e)

    return jsonify({
        "run": run,
        "findings_markdown": findings_markdown,
        "findings_frontmatter": findings_frontmatter,
        "findings_sections": findings_sections,
    })


@incident_subagents_bp.route(
    "/api/incidents/<incident_id>/subagents/<agent_id>/transcript", methods=["GET"]
)
@require_permission("incidents", "read")
def get_transcript(user_id, incident_id, agent_id):
    try:
        after_seq = int(request.args.get("after_seq", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "after_seq must be an integer"}), 400

    from utils.db.connection_pool import db_pool
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[IncidentSubagents:transcript]")
            if not _incident_exists(cur, incident_id):
                return jsonify({"error": "Incident not found"}), 404
            cur.execute(
                "SELECT session_id FROM incident_subagent_runs "
                "WHERE incident_id = %s AND agent_id = %s",
                (incident_id, agent_id),
            )
            run_row = cur.fetchone()
            if not run_row:
                return jsonify({"error": "Sub-agent run not found"}), 404
            session_id = run_row[0]

            cur.execute(
                "SELECT seq, type, payload, created_at FROM chat_events "
                "WHERE session_id = %s AND agent_id = %s AND seq > %s "
                "ORDER BY seq ASC LIMIT %s",
                (session_id, agent_id, after_seq, MAX_TRANSCRIPT_EVENTS + 1),
            )
            rows = cur.fetchall()

    truncated = len(rows) > MAX_TRANSCRIPT_EVENTS
    if truncated:
        rows = rows[:MAX_TRANSCRIPT_EVENTS]

    events = [
        {
            "seq": r[0],
            "type": r[1],
            "payload": r[2],
            "created_at": r[3].isoformat() if r[3] else None,
        }
        for r in rows
    ]

    response = {"events": events}
    if truncated:
        response["truncated"] = True
        response["next_seq"] = events[-1]["seq"] if events else after_seq

    return jsonify(response)
