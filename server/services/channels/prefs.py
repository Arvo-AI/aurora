"""Org-level incidents-channel prefs shared by every chat platform connector."""

from __future__ import annotations

import json
import logging
from typing import Optional, Set

from utils.auth.stateless_auth import (
    get_credentials_from_db,
    get_org_id_for_user,
    get_org_preference,
    store_org_preference,
)
from utils.auth.token_management import store_tokens_in_db
from utils.auth.stateless_auth import set_rls_context
from utils.db.connection_pool import db_pool
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)


def incidents_channel_pref_key(provider: str) -> str:
    return f"{provider}_incidents_channel_id"


def incidents_channel_name_pref_key(provider: str) -> str:
    return f"{provider}_incidents_channel_name"


def get_incidents_channel_id(user_id: str, provider: str) -> Optional[str]:
    """Creds first, then org preference — same resolution as Slack notifications."""
    try:
        creds = get_credentials_from_db(user_id, provider) or {}
        cid = creds.get("incidents_channel_id")
        if cid:
            return cid
        org_id = get_org_id_for_user(user_id)
        if org_id:
            return get_org_preference(org_id, incidents_channel_pref_key(provider)) or None
    except Exception:
        logger.debug("Could not resolve incidents channel for %s", provider, exc_info=True)
    return None


def set_incidents_channel(user_id: str, provider: str, channel_id: str) -> None:
    channel_name = _channel_name_from_db(user_id, provider, channel_id)
    org_id = get_org_id_for_user(user_id)
    if org_id:
        store_org_preference(org_id, incidents_channel_pref_key(provider), channel_id)
        store_org_preference(org_id, incidents_channel_name_pref_key(provider), channel_name)

    creds = get_credentials_from_db(user_id, provider) or {}
    creds["incidents_channel_id"] = channel_id
    creds["incidents_channel_name"] = channel_name
    store_tokens_in_db(user_id, creds, provider)


def clear_incidents_channel(user_id: str, provider: str) -> None:
    org_id = get_org_id_for_user(user_id)
    if org_id:
        store_org_preference(org_id, incidents_channel_pref_key(provider), "")
        store_org_preference(org_id, incidents_channel_name_pref_key(provider), "")
    creds = get_credentials_from_db(user_id, provider) or {}
    creds.pop("incidents_channel_id", None)
    creds.pop("incidents_channel_name", None)
    store_tokens_in_db(user_id, creds, provider)


def hidden_channels_pref_key(provider: str) -> str:
    """Channels the org opted out of (Teams has no bot-leave API)."""
    return f"{provider}_hidden_channel_ids"


def get_hidden_channel_ids(user_id: str, provider: str) -> Set[str]:
    org_id = get_org_id_for_user(user_id)
    if not org_id:
        return set()
    raw = get_org_preference(org_id, hidden_channels_pref_key(provider), default="[]")
    try:
        parsed = json.loads(raw) if raw else []
        return {cid for cid in parsed if isinstance(cid, str) and cid}
    except (TypeError, json.JSONDecodeError):
        return set()


def add_hidden_channel(user_id: str, provider: str, channel_id: str) -> None:
    org_id = get_org_id_for_user(user_id)
    if not org_id or not channel_id:
        return
    hidden = get_hidden_channel_ids(user_id, provider)
    hidden.add(channel_id)
    store_org_preference(org_id, hidden_channels_pref_key(provider), json.dumps(sorted(hidden)))


def remove_hidden_channel(user_id: str, provider: str, channel_id: str) -> None:
    org_id = get_org_id_for_user(user_id)
    if not org_id or not channel_id:
        return
    hidden = get_hidden_channel_ids(user_id, provider)
    if channel_id not in hidden:
        return
    hidden.discard(channel_id)
    store_org_preference(org_id, hidden_channels_pref_key(provider), json.dumps(sorted(hidden)))


def _channel_name_from_db(user_id: str, provider: str, channel_id: str) -> str:
    try:
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix=f"[channels:{provider}:cardname]")
            cur.execute(
                """SELECT channel_name FROM slack_channels
                   WHERE provider = %s AND channel_id = %s LIMIT 1""",
                (provider, channel_id),
            )
            row = cur.fetchone()
            return (row[0] if row else "") or ""
    except Exception:
        logger.debug("Could not read channel name for %s", sanitize(channel_id), exc_info=True)
        return ""
