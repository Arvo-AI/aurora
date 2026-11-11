"""Admin chat audit for the monitor page.

Lists every chat in the org (all users, all channels) with search, filters and
detected issues, and serves a read-only transcript of any one chat. Admin-only:
regular members only see their own chats in the chat UI. There is deliberately
no write endpoint here: an admin reads other users' chats, never posts into them.
"""
import logging

from flask import Blueprint, jsonify, request

from services.monitor.chat_audit import (
    ChatFilters,
    InvalidChatFilter,
    chat_filter_error_message,
    get_transcript,
    list_chats,
    validate_session_id,
)
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request, set_rls_context
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

chat_audit_bp = Blueprint("monitor_chat_audit", __name__)


@chat_audit_bp.route("/api/monitor/chats", methods=["GET"])
@require_permission("admin", "access")
def list_org_chats(user_id):
    """Query params: period (24h|7d|30d|90d), source, status (completed|failed|in_progress),
    issue (a category or "any"), user (user id), q (full-text over messages), page, per_page.
    """
    try:
        filters = ChatFilters.parse(request.args.to_dict())
    except InvalidChatFilter as exc:
        return jsonify({"error": chat_filter_error_message(exc)}), 400

    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No organization context"}), 403

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, org_id=org_id, log_prefix="[CHAT_AUDIT]")
                result = list_chats(cur, org_id, filters)
        return jsonify(result), 200
    except Exception:
        logger.exception("list_org_chats failed")
        return jsonify({"error": "Failed to list chats"}), 500


@chat_audit_bp.route("/api/monitor/chats/<session_id>", methods=["GET"])
@require_permission("admin", "access")
def get_org_chat(user_id, session_id):
    """Read-only transcript of one chat in the caller's org."""
    try:
        validate_session_id(session_id)
    except InvalidChatFilter as exc:
        return jsonify({"error": chat_filter_error_message(exc)}), 400

    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No organization context"}), 403

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, org_id=org_id, log_prefix="[CHAT_AUDIT]")
                transcript = get_transcript(cur, org_id, session_id)
        if transcript is None:
            return jsonify({"error": "Chat not found"}), 404
        return jsonify(transcript), 200
    except Exception:
        logger.exception("get_org_chat failed")
        return jsonify({"error": "Failed to load chat"}), 500
