"""Org choices for chats and for who may modify memory.

A missing chat preference keeps viewers from starting a chat. Editors and
admins can always chat. A missing memory preference keeps viewers from
editing memory and still lets editors. Admins can always edit memory.
"""

from __future__ import annotations

import logging

from utils.auth import ROLE_ADMIN, ROLE_EDITOR, ROLE_VIEWER
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

_CHAT_KEY = "viewer_can_chat"
_MEMORY_KEY = "viewer_can_edit_memory"
_EDITOR_MEMORY_KEY = "editor_can_edit_memory"
_DEFAULT = False


def flag_enabled(raw: object, default: bool) -> bool:
    if raw is None:
        return default
    return str(raw).strip().lower() not in ("false", "0", "no")


def viewer_only(roles: list) -> bool:
    names = {str(role) for role in roles if role}
    # Admin and editor inherit viewer, but they are assigned their own role
    return bool(names) and names.isdisjoint({ROLE_ADMIN, ROLE_EDITOR})


def _org_flag(org_id: str, key: str, default: bool = _DEFAULT) -> bool:
    if not org_id:
        return default
    try:
        from utils.db.connection_pool import db_pool

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT preference_value FROM user_preferences
                       WHERE user_id = '__org__' AND org_id = %s AND preference_key = %s""",
                    (org_id, key),
                )
                row = cur.fetchone()
        return flag_enabled(row[0] if row else None, default)
    except Exception:
        # A failed read must not grant an edit the org may have turned off.
        logger.exception("Could not read viewer preference %s for org %s", key, sanitize(org_id))
        return False


def org_allows_viewers(org_id: str) -> bool:
    return _org_flag(org_id, _CHAT_KEY)


def org_allows_memory_edits(org_id: str) -> bool:
    return _org_flag(org_id, _MEMORY_KEY)


def org_allows_editor_memory_edits(org_id: str) -> bool:
    # Unset stays on, so existing editors do not lose memory edits.
    return _org_flag(org_id, _EDITOR_MEMORY_KEY, True)


def _viewer_allowed(user_id: str, org_id: str, org_flag) -> bool:
    if not user_id or not org_id:
        return _DEFAULT
    from utils.auth.enforcer import get_user_roles_in_org

    # Editors and admins are not viewers, so the org switch does not apply.
    if not viewer_only(get_user_roles_in_org(user_id, org_id)):
        return True
    return org_flag(org_id)


def viewer_may_run(user_id: str, org_id: str) -> bool:
    return _viewer_allowed(user_id, org_id, org_allows_viewers)


def viewer_may_edit_memory(user_id: str, org_id: str) -> bool:
    if not user_id or not org_id:
        return False
    from utils.auth.connector_access import pinned_roles
    from utils.auth.enforcer import get_user_roles_in_org

    pinned = pinned_roles()
    # A Slack guest keeps the connector owner's id. The pin is the role to check.
    if pinned is not None:
        roles = pinned
    else:
        roles = get_user_roles_in_org(user_id, org_id)
    names = {str(role) for role in roles if role}
    # Admins stay allowed so an org cannot lock itself out of memory.
    if ROLE_ADMIN in names:
        return True
    # Editors follow their own switch. Viewers follow the other one.
    if ROLE_EDITOR in names:
        return org_allows_editor_memory_edits(org_id)
    if ROLE_VIEWER in names:
        return org_allows_memory_edits(org_id)
    return False


def message_starts_chat(question, data: dict) -> bool:
    """True when this websocket message starts work the viewer switch covers."""
    # An action, an RCA request, or a file with no question still starts a chat.
    return bool(
        question
        or data.get("direct_tool_call")
        or data.get("trigger_action")
        or data.get("trigger_rca")
        or data.get("attachments")
    )


def slack_caller_may_run(org_id: str, user_id: str, *, unlinked: bool = False) -> bool:
    """An Aurora account mentioning Aurora. Guests are decided before this."""
    # No account on this call. The Slack manage toggle is what lets them in.
    if unlinked:
        return False
    return viewer_may_run(user_id, org_id)


def _write_flag(cur, org_id: str, key: str, enabled: bool) -> None:
    cur.execute(
        """INSERT INTO user_preferences (user_id, org_id, preference_key, preference_value)
           VALUES ('__org__', %s, %s, %s)
           ON CONFLICT (user_id, org_id, preference_key)
           DO UPDATE SET preference_value = EXCLUDED.preference_value""",
        (org_id, key, "true" if enabled else "false"),
    )


def save_viewer_run(
    org_id: str,
    user_id: str,
    chat: bool | None = None,
    memory: bool | None = None,
    editor_memory: bool | None = None,
) -> None:
    from utils.auth.stateless_auth import set_rls_context
    from utils.db.connection_pool import db_pool

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[ViewerRun]")
            # Each switch is stored on its own. Omitting one leaves it as it was.
            if chat is not None:
                _write_flag(cur, org_id, _CHAT_KEY, chat)
            if memory is not None:
                _write_flag(cur, org_id, _MEMORY_KEY, memory)
            if editor_memory is not None:
                _write_flag(cur, org_id, _EDITOR_MEMORY_KEY, editor_memory)
        conn.commit()
