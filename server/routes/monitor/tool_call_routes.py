"""Tool-call search for the monitor page (DEV-1729).

Lists the agent's tool calls across every chat and RCA in the org, filterable by
status, time window, tool, source and free text, so failures can be reviewed
without opening each chat. Query logic lives in
``services.monitor.tool_call_search`` and is shared with the agent's
``search_tool_calls`` tool.
"""
import logging

from flask import Blueprint, jsonify, request

from services.monitor.tool_call_search import (
    InvalidFilter,
    ToolCallFilters,
    search_tool_calls,
)
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request, set_rls_context
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

tool_calls_bp = Blueprint("monitor_tool_calls", __name__)


@tool_calls_bp.route("/api/monitor/tool-calls", methods=["GET"])
@require_permission("incidents", "read")
def list_tool_calls(user_id):
    """Search tool calls.

    Query params: status (failed|succeeded|running|all), period (24h|7d|30d|90d),
    since/until (ISO-8601, override period), tool, source (chat|incident|all),
    q (free text over tool name, input and error), page, per_page (<= 100).
    """
    try:
        filters = ToolCallFilters.parse(request.args.to_dict())
    except InvalidFilter as exc:
        return jsonify({"error": str(exc)}), 400

    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No organization context"}), 403

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, org_id=org_id, log_prefix="[TOOL_CALLS]")
                result = search_tool_calls(cur, org_id, filters)
        return jsonify(result), 200
    except Exception:
        logger.exception("list_tool_calls failed")
        return jsonify({"error": "Failed to search tool calls"}), 500
