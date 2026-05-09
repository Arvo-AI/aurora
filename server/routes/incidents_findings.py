"""RCA findings routes — list sub-agent findings and fetch finding bodies.

Full RBAC + RLS. Registered in main_compute.py near the existing incidents routes.
"""

import logging
import re
from uuid import UUID

from flask import Blueprint, jsonify

from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import set_rls_context
from utils.db.connection_pool import db_pool
from utils.log_sanitizer import hash_for_log, sanitize
from utils.storage.storage import get_storage_manager

logger = logging.getLogger(__name__)

findings_bp = Blueprint("rca_findings", __name__)
_LOG_PREFIX = "[Findings]"
_AGENT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

# Keep in sync with _OUTPUT_EXCERPT_MAX_CHARS in tool_context_capture.py and
# _MAX_HISTORY_ENTRIES / _entry_command's default limit in sub_agent.py — live
# and archived tool_call_history entries must render identically.
_OUTPUT_EXCERPT_MAX_CHARS = 1000
_COMMAND_MAX_CHARS = 1024
_MAX_HISTORY_ENTRIES = 30

_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "timeout", "cancelled", "inconclusive"})

_PROVIDER_CLI = {
    "aws": "aws",
    "gcp": "gcloud", "gcloud": "gcloud",
    "azure": "az", "az": "az",
    "ovh": "ovhcloud", "ovhcloud": "ovhcloud",
    "scaleway": "scw", "scw": "scw",
}
_RECOGNIZED_CLI_PREFIXES = (
    "aws ", "gcloud ", "gsutil ", "bq ", "az ",
    "ovhcloud ", "scw ",
    "kubectl ", "helm ", "docker ",
)


def _validate_uuid(value: str) -> bool:
    try:
        UUID(value)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _excerpt(output) -> str:
    if not output:
        return ""
    s = output if isinstance(output, str) else str(output)
    if len(s) > _OUTPUT_EXCERPT_MAX_CHARS:
        return s[:_OUTPUT_EXCERPT_MAX_CHARS] + "...[truncated]"
    return s


def _truncate(s: str, limit: int) -> str:
    return s if len(s) <= limit else s[:limit] + "...[truncated]"


def _derive_command(tool_input) -> str:
    if not isinstance(tool_input, dict):
        return ""
    cmd = tool_input.get("command")
    if isinstance(cmd, str) and cmd.strip():
        provider = tool_input.get("provider")
        if provider:
            cli = _PROVIDER_CLI.get(str(provider).lower())
            if cli and not cmd.lstrip().startswith(_RECOGNIZED_CLI_PREFIXES):
                cmd = f"{cli} {cmd.lstrip()}"
        return _truncate(cmd, _COMMAND_MAX_CHARS)
    for key in ("query", "path", "promql"):
        v = tool_input.get(key)
        if v:
            return _truncate(str(v), _COMMAND_MAX_CHARS)
    return ""


def _build_history_from_steps(rows) -> list:
    return [
        {
            "tool_name": tool_name or "unknown",
            "args": tool_input,
            "command": _derive_command(tool_input),
            "output_excerpt": _excerpt(tool_output),
            "is_error": status == "error",
            "status": status,
            "started_at": started_at.isoformat() if started_at else None,
            "completed_at": completed_at.isoformat() if completed_at else None,
        }
        for tool_name, tool_input, tool_output, status, started_at, completed_at in rows
    ]


@findings_bp.route("/api/incidents/<incident_id>/findings", methods=["GET"])
@require_permission("incidents", "read")
def list_findings(user_id, incident_id: str):
    if not _validate_uuid(incident_id):
        return jsonify({"error": "Invalid incident ID format"}), 400

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix=_LOG_PREFIX)
                cursor.execute(
                    """
                    SELECT agent_id, role_name, purpose, status, self_assessed_strength,
                           current_action, child_session_id, started_at, completed_at,
                           tools_used, citations, follow_ups_suggested, wave
                    FROM rca_findings
                    WHERE incident_id = %s
                    ORDER BY started_at ASC
                    """,
                    (incident_id,),
                )
                cols = [d[0] for d in cursor.description]
                rows = cursor.fetchall()

        findings = []
        for row in rows:
            d = dict(zip(cols, row))
            findings.append({
                "agent_id": d["agent_id"],
                "role_name": d["role_name"],
                "purpose": d["purpose"],
                "status": d["status"],
                "wave": d.get("wave"),
                "self_assessed_strength": d.get("self_assessed_strength"),
                "current_action": d.get("current_action"),
                "child_session_id": d.get("child_session_id"),
                "started_at": d["started_at"].isoformat() if d.get("started_at") else None,
                "completed_at": d["completed_at"].isoformat() if d.get("completed_at") else None,
                "tools_used": d.get("tools_used") or [],
                "citations": d.get("citations") or [],
                "follow_ups_suggested": d.get("follow_ups_suggested") or [],
            })

        logger.info(
            "%s list_findings: incident=%s count=%d",
            _LOG_PREFIX, hash_for_log(incident_id), len(findings),
        )
        return jsonify({"findings": findings}), 200

    except Exception:
        logger.exception(
            "%s list_findings failed for incident %s",
            _LOG_PREFIX, hash_for_log(incident_id),
        )
        return jsonify({"error": "Failed to retrieve findings"}), 500


@findings_bp.route("/api/incidents/<incident_id>/findings/<agent_id>", methods=["GET"])
@require_permission("incidents", "read")
def get_finding_body(user_id, incident_id: str, agent_id: str):
    if not _validate_uuid(incident_id):
        return jsonify({"error": "Invalid incident ID format"}), 400
    if not _AGENT_ID_RE.match(agent_id):
        return jsonify({"error": "Invalid agent ID format"}), 400

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix=_LOG_PREFIX)
                cursor.execute(
                    "SELECT storage_uri, status, tool_call_history, user_id "
                    "FROM rca_findings WHERE incident_id = %s AND agent_id = %s",
                    (incident_id, agent_id),
                )
                row = cursor.fetchone()
                if not row:
                    return jsonify({"error": "Finding not found"}), 404

                storage_uri, status, tool_call_history, originator_id = row[0], row[1], row[2], row[3]

                # Read from execution_steps so the UI can render running tool calls
                # before the sub-agent terminates and persists the JSONB blob.
                # Suffix-match: child_session_id on rca_findings is NULL for
                # in-flight sub-agents, so we can't use exact session_id =.
                # +1 char so _excerpt still detects overflow and appends "...[truncated]".
                cursor.execute(
                    """
                    SELECT tool_name, tool_input,
                           LEFT(tool_output, %s) AS tool_output,
                           status, started_at, completed_at
                    FROM execution_steps
                    WHERE incident_id = %s
                      AND session_id LIKE %s
                      AND tool_name <> 'write_findings'
                    ORDER BY step_index ASC
                    LIMIT %s
                    """,
                    (
                        _OUTPUT_EXCERPT_MAX_CHARS + 1,
                        incident_id,
                        f"%::{agent_id}",
                        _MAX_HISTORY_ENTRIES,
                    ),
                )
                step_rows = cursor.fetchall()

        history = _build_history_from_steps(step_rows)
        # Fall back to the archived JSONB only when terminal — covers old
        # incidents whose execution_steps rows have been pruned.
        if not history and status in _TERMINAL_STATUSES:
            history = tool_call_history or []
        if not storage_uri:
            # Body not yet written. Return 200 with status so the client can keep
            # polling until terminal, instead of mistaking a 404 for a hard miss.
            return jsonify({
                "agent_id": agent_id,
                "status": status,
                "body": None,
                "tool_call_history": history,
            }), 200

        # Storage path is user-scoped under the RCA originator; co-org viewers
        # (RBAC-allowed) must read with the originator's id or the prefix mismatches.
        storage_user_id = originator_id or user_id
        try:
            data = get_storage_manager(storage_user_id).download_bytes(storage_uri, storage_user_id)
            if not isinstance(data, bytes):
                logger.error(
                    "%s storage returned non-bytes for agent=%s incident=%s: %s",
                    _LOG_PREFIX, sanitize(agent_id), hash_for_log(incident_id),
                    type(data).__name__,
                )
                return jsonify({"error": "Failed to retrieve finding body"}), 500
            body = data.decode("utf-8")
        except Exception:
            logger.exception(
                "%s failed to fetch finding body for agent=%s incident=%s",
                _LOG_PREFIX, sanitize(agent_id), hash_for_log(incident_id),
            )
            return jsonify({"error": "Failed to retrieve finding body"}), 500

        return jsonify({
            "agent_id": agent_id,
            "status": status,
            "body": body,
            "tool_call_history": history,
        }), 200

    except Exception:
        logger.exception(
            "%s get_finding_body failed for agent=%s incident=%s",
            _LOG_PREFIX, sanitize(agent_id), hash_for_log(incident_id),
        )
        return jsonify({"error": "Failed to retrieve finding"}), 500
