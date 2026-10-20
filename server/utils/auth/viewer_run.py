"""Org choice: may a viewer start a conversation.

That includes a new chat in Aurora and a conversation started from Slack.
Editors and admins are unaffected. A missing preference keeps viewers from
starting one, which is how the app already treats them.
"""

from __future__ import annotations

import logging

from utils.auth import ROLE_ADMIN, ROLE_EDITOR
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

_KEY = "viewer_can_chat"
_DEFAULT = False


def flag_enabled(raw: object, default: bool) -> bool:
    if raw is None:
        return default
    return str(raw).strip().lower() not in ("false", "0", "no")


def viewer_only(roles: list) -> bool:
    names = {str(role) for role in roles if role}
    # Admin and editor inherit viewer, but they are assigned their own role
    return bool(names) and names.isdisjoint({ROLE_ADMIN, ROLE_EDITOR})


def org_allows_viewers(org_id: str) -> bool:
    if not org_id:
        return _DEFAULT
    try:
        from utils.db.connection_pool import db_pool

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT preference_value FROM user_preferences
                       WHERE user_id = '__org__' AND org_id = %s AND preference_key = %s""",
                    (org_id, _KEY),
                )
                row = cur.fetchone()
        return flag_enabled(row[0] if row else None, _DEFAULT)
    except Exception:
        logger.exception("Could not read viewer chat preference for org %s", sanitize(org_id))
        return _DEFAULT


def viewer_may_run(user_id: str, org_id: str) -> bool:
    if not user_id or not org_id:
        return _DEFAULT
    from utils.auth.enforcer import get_user_roles_in_org

    if not viewer_only(get_user_roles_in_org(user_id, org_id)):
        return True
    return org_allows_viewers(org_id)


def slack_caller_may_run(org_id: str, user_id: str, *, unlinked: bool) -> bool:
    """A Slack mention starts a chat. Unlinked members run as the workspace."""
    # No Aurora account: the existing workspace identity, not a viewer.
    if unlinked:
        return True
    return viewer_may_run(user_id, org_id)


def save_viewer_run(org_id: str, user_id: str, chat: bool) -> None:
    from utils.auth.stateless_auth import set_rls_context
    from utils.db.connection_pool import db_pool

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[ViewerRun]")
            cur.execute(
                """INSERT INTO user_preferences (user_id, org_id, preference_key, preference_value)
                   VALUES ('__org__', %s, %s, %s)
                   ON CONFLICT (user_id, org_id, preference_key)
                   DO UPDATE SET preference_value = EXCLUDED.preference_value""",
                (org_id, _KEY, "true" if chat else "false"),
            )
        conn.commit()
