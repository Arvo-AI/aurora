"""Which role may use a connector or account, for everyone or one team.

No rows means everyone. A team's rows for a connector replace the everyone
rules for members of that team. A connector the team has not saved still follows everyone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Optional

from utils.auth import ROLE_ADMIN, ROLE_EDITOR, ROLE_VIEWER, VALID_ROLES
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

# Stored when an admin limits a connector and then grants it to nobody.
# A real role never matches it, so the connector stays closed.
RESTRICTED_MARKER = "*"

CREDENTIAL_WRITE = "write"
CREDENTIAL_READ = "read"
SCOPED_CREDENTIALS = frozenset({CREDENTIAL_WRITE, CREDENTIAL_READ, ""})

ROLES = (ROLE_VIEWER, ROLE_EDITOR, ROLE_ADMIN)


@dataclass(frozen=True)
class Grant:
    connector: str
    scope_id: str
    credential: str
    role: str
    team_id: str = ""


def rule_signature(grants: Iterable[Grant]) -> frozenset:
    return frozenset(
        (g.scope_id, g.credential, g.role) for g in grants if g.role != RESTRICTED_MARKER
    )


def inherits_general(general_rows: Iterable[Grant], limited: bool, grants: Iterable[Grant]) -> bool:
    """A team save that matches everyone should not be stored as an override."""
    general = list(general_rows)
    general_limited = bool(general)
    if not limited and not general_limited:
        return True
    if not limited or not general_limited:
        return False
    return rule_signature(grants) == rule_signature(general)


def is_limited(grants: Iterable[Grant], connector: str) -> bool:
    return any(g.connector == connector for g in grants)


def credentials_for(
    grants: Iterable[Grant],
    connector: str,
    user_roles: Iterable[str],
    scope_id: str = "",
) -> Optional[frozenset]:
    """Credentials this role may use, or None when the connector is not limited.

    An empty set means the connector is limited and this role has no grant.
    scope_id "" asks about the connector as a whole (any scope counts).
    A grant with an empty scope applies to every scope.
    """
    rows = [g for g in grants if g.connector == connector]
    if not rows:
        return None
    roles = set(user_roles)
    matched = []
    for grant in rows:
        if grant.role not in roles:
            continue
        # A whole-connector question is yes if any scope was granted.
        if scope_id and grant.scope_id not in ("", scope_id):
            continue
        matched.append(grant)
    return frozenset(g.credential for g in matched)


def may_use_roles(
    grants: Iterable[Grant], connector: str, user_roles: Iterable[str], scope_id: str = "",
) -> bool:
    creds = credentials_for(grants, connector, user_roles, scope_id)
    return creds is None or bool(creds)


def select_aws_role(conn: dict, creds: Optional[frozenset], ask_mode: bool):
    """Which role ARN to assume, and whether to add a read-only session policy.

    Returns (role_arn, use_session_policy). role_arn None means this account
    is off limits for the caller. creds None means the connector is not limited,
    so the chat mode decides, which is the previous behavior.
    """
    write = conn.get("role_arn")
    read = conn.get("read_only_role_arn")
    if not write:
        return None, False
    # Not limited: Ask mode prefers the read-only role, and falls back to a
    # session policy on the modify role when that role was never configured.
    if creds is None:
        if ask_mode and read:
            return read, False
        return write, ask_mode
    if not creds:
        return None, False
    # A read grant never assumes the modify role, including in Agent mode.
    if CREDENTIAL_WRITE not in creds:
        if read:
            return read, False
        return write, True
    # Write grant. Ask mode still prefers the read-only role, and otherwise
    # intersects the modify role with a read-only session policy.
    if ask_mode:
        if read:
            return read, False
        return write, True
    return write, False


def gcp_token_mode(creds: Optional[frozenset], chat_mode: str) -> Optional[str]:
    """SA selection for GCP. None keeps the chat mode. "ask" forces the
    read-only service account. "deny" means this role has no grant.
    """
    if creds is None:
        return None
    if not creds:
        return "deny"
    # Read-only grant: never hand out the modify service account.
    if CREDENTIAL_WRITE not in creds:
        return "ask"
    return chat_mode if chat_mode in ("ask", "agent") else None


def _roles_for(user_id: str) -> list:
    from utils.auth.enforcer import get_user_roles_in_org
    from utils.auth.stateless_auth import get_org_id_for_user

    org_id = get_org_id_for_user(user_id)
    if not org_id:
        return []
    try:
        return list(get_user_roles_in_org(user_id, org_id) or [])
    except Exception:
        logger.warning("Connector access role lookup failed for user %s", sanitize(user_id))
        return []


def load_grants(user_id: str) -> list:
    """Grants for the caller's org. Empty when nothing is limited or the read fails.

    A failed read stays open so a missing table cannot take every connector
    offline. Limiting only starts once rows exist.
    """
    from utils.auth.stateless_auth import get_org_id_for_user, set_rls_context
    from utils.db.connection_pool import db_pool

    org_id = get_org_id_for_user(user_id)
    if not org_id:
        return []
    try:
        with db_pool.get_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[ConnectorAccess]")
                cur.execute(
                    """SELECT connector, scope_id, credential, role
                         FROM org_connector_access
                        WHERE org_id = %s AND team_id = ''""",
                    (org_id,),
                )
                return [Grant(r[0], r[1] or "", r[2] or "", r[3]) for r in cur.fetchall()]
    except Exception:
        logger.warning("Connector access load failed for user %s", sanitize(user_id))
        return []


class Access:
    """One user's grants, loaded once per tool build."""

    def __init__(self, grants: list, roles: list, team_rules: list | None = None, team_ids: Iterable[str] | None = None):
        self.grants = grants
        self.roles = roles
        self.team_rules = list(team_rules or [])
        self.team_ids = set(team_ids or [])

    def credentials(self, connector: str, scope_id: str = "") -> Optional[frozenset]:
        # Saved team rows replace the everyone rules for that connector.
        override = [g for g in self.team_rules if g.connector == connector and g.team_id in self.team_ids]
        if override:
            return credentials_for(override, connector, self.roles, scope_id)
        if not is_limited(self.grants, connector):
            return None
        return credentials_for(self.grants, connector, self.roles, scope_id)

    def may_use(self, connector: str, scope_id: str = "") -> bool:
        creds = self.credentials(connector, scope_id)
        return creds is None or bool(creds)

    def checked_as(self, team_names: dict | None = None) -> str:
        """'general viewer' with no team, or 'Database viewer' for a team member."""
        role = _role_phrase(self.roles)
        names = team_names or {}
        # The label is who they are, even when this connector still follows everyone.
        mine = sorted(self.team_ids)
        if mine:
            team = names.get(mine[0]) or "your team"
            return f"{team} {role}".strip()
        if not role:
            return ""
        return f"general {role}"

    def revision(self) -> str:
        role_part = "open" if not self.grants else ",".join(sorted(
            f"{g.connector}/{g.scope_id}/{g.credential}/{g.role}" for g in self.grants
        ))
        team_part = ",".join(sorted(self.team_ids)) + ":" + ",".join(sorted(
            f"{g.team_id}/{g.connector}/{g.scope_id}/{g.credential}/{g.role}" for g in self.team_rules
        ))
        return f"{role_part}|{team_part}"


def load_team_rules(user_id: str) -> tuple:
    """(team ids this user belongs to, that team's connector rules).

    A failed read returns nothing, so members keep the everyone rules.
    """
    from utils.auth.stateless_auth import get_org_id_for_user, set_rls_context
    from utils.db.connection_pool import db_pool

    org_id = get_org_id_for_user(user_id)
    if not org_id:
        return set(), []
    try:
        with db_pool.get_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[TeamAccess]")
                cur.execute(
                    """SELECT team_id FROM org_team_members
                        WHERE org_id = %s AND user_id = %s""",
                    (org_id, user_id),
                )
                mine = {row[0] for row in cur.fetchall()}
                if not mine:
                    return set(), []
                cur.execute(
                    """SELECT team_id, connector, scope_id, credential, role
                         FROM org_connector_access
                        WHERE org_id = %s AND team_id = ANY(%s)""",
                    (org_id, list(mine)),
                )
                grants = [Grant(r[1], r[2] or "", r[3] or "", r[4], r[0]) for r in cur.fetchall()]
                return mine, grants
    except Exception:
        logger.warning("Team access load failed for user %s", sanitize(user_id))
        return set(), []


def load_team_grants(user_id: str, team_id: str) -> list:
    """Rules saved for one team. Empty when that team has not overridden anything."""
    from utils.auth.stateless_auth import get_org_id_for_user, set_rls_context
    from utils.db.connection_pool import db_pool

    org_id = get_org_id_for_user(user_id)
    if not org_id or not team_id:
        return []
    try:
        with db_pool.get_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[TeamAccess]")
                cur.execute(
                    """SELECT connector, scope_id, credential, role
                         FROM org_connector_access
                        WHERE org_id = %s AND team_id = %s""",
                    (org_id, team_id),
                )
                return [Grant(r[0], r[1] or "", r[2] or "", r[3], team_id) for r in cur.fetchall()]
    except Exception:
        logger.warning("Team grant load failed for user %s", sanitize(user_id))
        return []


def access_for(user_id: str) -> Access:
    grants = load_grants(user_id)
    team_ids, team_rules = load_team_rules(user_id)
    # Role lookup only matters once an admin has limited something.
    roles = _roles_for(user_id) if grants or team_rules else []
    return Access(grants, roles, team_rules, team_ids)


def _role_phrase(roles: Iterable[str]) -> str:
    have = set(roles)
    known = [role for role in ROLES if role in have]
    # One org role is the normal case.
    if len(known) == 1:
        return known[0]
    # More than one assigned role is named together.
    if known:
        return " and ".join(known)
    return ""


def denial_message(connector: str, audience: str) -> str:
    """What the tool tells the model when this caller cannot use the connector."""
    who = f" as a {audience}" if audience else ""
    return (
        f"You don't have access to {connector}{who}. "
        "Ask an admin to change this permission in org Settings, on the Access tab."
    )


def _team_names(user_id: str, team_ids: set) -> dict:
    from utils.auth.stateless_auth import get_org_id_for_user, set_rls_context
    from utils.db.connection_pool import db_pool

    org_id = get_org_id_for_user(user_id)
    if not org_id or not team_ids:
        return {}
    try:
        with db_pool.get_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[TeamAccess]")
                cur.execute(
                    "SELECT id, name FROM org_teams WHERE org_id = %s AND id = ANY(%s)",
                    (org_id, list(team_ids)),
                )
                return {row[0]: row[1] for row in cur.fetchall() if row[1]}
    except Exception:
        logger.warning("Team name lookup failed during connector denial")
        return {}


def denial_text(user_id: str, connector: str) -> str:
    access = access_for(user_id)
    names = _team_names(user_id, access.team_ids) if access.team_ids else {}
    return denial_message(connector, access.checked_as(names))


def may_use(user_id: str, connector: str, scope_id: str = "") -> bool:
    return access_for(user_id).may_use(connector, scope_id)


def credentials_for_user(user_id: str, connector: str, scope_id: str = "") -> Optional[frozenset]:
    return access_for(user_id).credentials(connector, scope_id)
