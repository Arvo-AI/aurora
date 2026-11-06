"""Org choices for chats and for who may modify memory.

Unset viewer chat stays on so an upgrade does not lock viewers out. An
explicit false turns it off. Editors and admins can always chat. Memory
edits follow their own switches; admins can always edit memory.
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


def _org_flag_state(org_id: str, key: str, default: bool = _DEFAULT) -> bool | None:
    """The org switch, or None when it could not be read."""
    if not org_id:
        return default
    try:
        from utils.db.connection_pool import db_pool

        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            from utils.auth.stateless_auth import _set_org_rls
            # Celery has no request, so the pool never sets the org on its own.
            _set_org_rls(cur, org_id)
            cur.execute(
                """SELECT preference_value FROM user_preferences
                   WHERE user_id = '__org__' AND org_id = %s AND preference_key = %s""",
                (org_id, key),
            )
            row = cur.fetchone()
        return flag_enabled(row[0] if row else None, default)
    except Exception:
        logger.exception("Could not read viewer preference %s for org %s", key, sanitize(org_id))
        return None


def _org_flag(org_id: str, key: str, default: bool = _DEFAULT) -> bool:
    """The org switch. A failed read must not grant an edit the org may have turned off."""
    return _org_flag_state(org_id, key, default) is True


def org_allows_viewers(org_id: str) -> bool:
    # Unset stays on, so existing viewers do not lose chat on upgrade.
    return _org_flag(org_id, _CHAT_KEY, True)


def _memory_switch(org_id: str, role: str) -> bool | None:
    """The memory switch for an editor or viewer, or None when it could not be read."""
    if role == ROLE_EDITOR:
        # Unset stays on, so existing editors do not lose memory edits.
        return _org_flag_state(org_id, _EDITOR_MEMORY_KEY, True)
    return _org_flag_state(org_id, _MEMORY_KEY)


def org_allows_memory_edits(org_id: str) -> bool:
    """True when the org lets viewers modify memory."""
    return _memory_switch(org_id, ROLE_VIEWER) is True


def org_allows_editor_memory_edits(org_id: str) -> bool:
    """True when the org lets editors modify memory."""
    return _memory_switch(org_id, ROLE_EDITOR) is True


def _viewer_allowed(user_id: str, org_id: str, org_flag) -> bool:
    if not user_id or not org_id:
        return _DEFAULT
    from utils.auth.enforcer import get_user_roles_in_org

    roles = get_user_roles_in_org(user_id, org_id) or []
    # No assigned role is not an editor. Chat and Slack have no other gate.
    if not roles:
        return False
    # Editors and admins are not viewers, so the org switch does not apply.
    if not viewer_only(roles):
        return True
    return org_flag(org_id)


def viewer_may_run(user_id: str, org_id: str) -> bool:
    return _viewer_allowed(user_id, org_id, org_allows_viewers)


def _memory_role(user_id: str, org_id: str) -> tuple[str | None, bool]:
    """The role memory edits are checked against, and whether it is a Slack guest pin."""
    from utils.auth.connector_access import pinned_roles
    from utils.auth.enforcer import get_user_roles_in_org

    pinned = pinned_roles()
    # A Slack guest keeps the connector owner's id. The pin is the role to check.
    roles = pinned if pinned is not None else get_user_roles_in_org(user_id, org_id)
    names = {str(role) for role in roles or [] if role}
    for role in (ROLE_ADMIN, ROLE_EDITOR, ROLE_VIEWER):
        if role in names:
            return role, pinned is not None
    return None, pinned is not None


def viewer_may_edit_memory(user_id: str, org_id: str) -> bool:
    """True when this person's role may modify memory under the org's switches."""
    if not user_id or not org_id:
        return False
    role, _ = _memory_role(user_id, org_id)
    # Admins stay allowed so an org cannot lock itself out of memory.
    if role == ROLE_ADMIN:
        return True
    # Editors follow their own switch. Viewers follow the other one.
    if role == ROLE_EDITOR:
        return org_allows_editor_memory_edits(org_id)
    if role == ROLE_VIEWER:
        return org_allows_memory_edits(org_id)
    return False


_SWITCHES = {ROLE_EDITOR: "Editors can modify memories", ROLE_VIEWER: "Viewers can modify memories"}


def memory_edit_refusal(user_id: str, org_id: str) -> str | None:
    """Why this person may not modify memory, or None when they may.

    The agent repeats this to the user, so each cause gets its own wording: a
    role the org switch excludes, no role, an unknown organization, or a switch
    that could not be read. A bare "not allowed" reads as an org-wide setting.
    """
    from utils.auth.connector_access import denial_message

    if not user_id or not org_id:
        # The org lookup returns nothing both when it is missing and when it fails.
        return (
            "Aurora could not determine this person's organization, so memory was not "
            "modified. Try again. If it keeps failing, check that their account belongs "
            "to an organization."
        )
    role, guest = _memory_role(user_id, org_id)
    # Admins stay allowed so an org cannot lock itself out of memory.
    if role == ROLE_ADMIN:
        return None
    if role not in _SWITCHES:
        return (
            "You don't have access to modify memories because you have no role in this "
            "organization. Ask an admin to assign you one in org Settings."
        )
    allowed = _memory_switch(org_id, role)
    if allowed:
        return None
    switch = _SWITCHES[role]
    if allowed is None:
        return (
            f"Aurora could not read the '{switch}' setting, so memory was not modified. "
            "This is a temporary read failure, not a permission change. Try again shortly."
        )
    # Same wording as a refused connector, so every Access-tab refusal reads alike.
    audience = "general viewer" if guest and role == ROLE_VIEWER else role
    return f"{denial_message('modify memories', audience)} The switch is '{switch}'."


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
