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

logger = logging.getLogger(__name__)

findings_bp = Blueprint("rca_findings", __name__)
_LOG_PREFIX = "[Findings]"
_AGENT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _validate_uuid(value: str) -> bool:
    try:
        UUID(value)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


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
                    "SELECT storage_uri, status, tool_call_history "
                    "FROM rca_findings WHERE incident_id = %s AND agent_id = %s",
                    (incident_id, agent_id),
                )
                row = cursor.fetchone()

        if not row:
            return jsonify({"error": "Finding not found"}), 404

        storage_uri, status, tool_call_history = row[0], row[1], row[2]
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

        try:
            from utils.storage.storage import get_storage_manager
            data = get_storage_manager(user_id).download_bytes(storage_uri, user_id)
            body = data.decode("utf-8") if isinstance(data, bytes) else str(data)
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
