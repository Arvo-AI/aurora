"""Shared metadata row updates for every chat platform (``slack_channels`` table)."""

from __future__ import annotations

from utils.auth.stateless_auth import set_rls_context
from utils.db.connection_pool import db_pool


def update_metadata(
    user_id: str,
    provider: str,
    channel_id: str,
    summary,
    status: str,
    channel_type: str | None = None,
    platform: str | None = None,
) -> None:
    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        if not set_rls_context(cur, conn, user_id, log_prefix=f"[ChannelMeta:{provider}]"):
            return
        if summary is None:
            cur.execute(
                """UPDATE slack_channels
                   SET metadata_status = %s, updated_at = NOW()
                   WHERE provider = %s AND channel_id = %s
                     AND metadata_status IN ('pending', 'generating')""",
                (status, provider, channel_id),
            )
        else:
            cur.execute(
                """UPDATE slack_channels
                   SET metadata_summary = %s, metadata_status = %s,
                       channel_type = COALESCE(%s, channel_type),
                       detected_platform = COALESCE(%s, detected_platform),
                       updated_at = NOW()
                   WHERE provider = %s AND channel_id = %s
                     AND metadata_status IN ('pending', 'generating')""",
                (summary, status, channel_type, platform, provider, channel_id),
            )
        conn.commit()


def claim_for_generation(
    user_id: str,
    provider: str,
    channel_id: str,
    *,
    allow_generating: bool = False,
) -> bool:
    statuses = ["pending", "skipped"] + (["generating"] if allow_generating else [])
    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        if not set_rls_context(cur, conn, user_id, log_prefix=f"[ChannelMeta:{provider}]"):
            return False
        cur.execute(
            """UPDATE slack_channels
               SET metadata_status = 'generating', updated_at = NOW()
               WHERE provider = %s AND channel_id = %s
                 AND metadata_status = ANY(%s)""",
            (provider, channel_id, statuses),
        )
        claimed = cur.rowcount > 0
        conn.commit()
    return claimed
