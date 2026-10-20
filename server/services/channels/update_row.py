"""Single-row UPDATE helper for ``slack_channels`` (all providers)."""

from __future__ import annotations

import logging

from flask import jsonify

from services.channels.registry import _check_provider
from utils.auth.stateless_auth import set_rls_context
from utils.db.connection_pool import db_pool
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)


def update_one_channel(user_id: str, provider: str, channel_id: str, set_clause: str, params: tuple):
    _check_provider(provider)
    try:
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix=f"[channels:{provider}:update]")
            cur.execute(
                f"""UPDATE slack_channels SET {set_clause}, updated_at = NOW()
                    WHERE provider = %s AND channel_id = %s""",
                (*params, provider, channel_id),
            )
            if cur.rowcount == 0:
                conn.rollback()
                return jsonify({"error": "Channel not found"}), 404
            conn.commit()
        return None
    except Exception:
        logger.exception("Error updating %s channel %s", provider, sanitize(channel_id))
        return jsonify({"error": "Failed to update channel"}), 500
