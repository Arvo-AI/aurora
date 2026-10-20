"""Which Aurora role may use which connector, account, and credential.

No rows for a connector means everyone can use it, which is how every
connector behaved before this existed. Saving a limit replaces that with
an explicit grant per role. A role is not implied by another role: viewers
can be given the read-only service account while admins get the one that
can modify.

A grant is (connector, scope, credential, role).

* scope "" is the whole connector. A scope id is one account or project.
* credential "" is the connector's only credential. "write" and "read" are
  the modify and read-only identities (AWS role ARNs, GCP service accounts).
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
                        WHERE org_id = %s""",
                    (org_id,),
                )
                return [Grant(r[0], r[1] or "", r[2] or "", r[3]) for r in cur.fetchall()]
    except Exception:
        logger.warning("Connector access load failed for user %s", sanitize(user_id))
        return []


class Access:
    """One user's grants, loaded once per tool build."""

    def __init__(self, grants: list, roles: list):
        self.grants = grants
        self.roles = roles

    def credentials(self, connector: str, scope_id: str = "") -> Optional[frozenset]:
        if not is_limited(self.grants, connector):
            return None
        return credentials_for(self.grants, connector, self.roles, scope_id)

    def may_use(self, connector: str, scope_id: str = "") -> bool:
        creds = self.credentials(connector, scope_id)
        return creds is None or bool(creds)

    def revision(self) -> str:
        if not self.grants:
            return "open"
        return ",".join(sorted(
            f"{g.connector}/{g.scope_id}/{g.credential}/{g.role}" for g in self.grants
        ))


def access_for(user_id: str) -> Access:
    grants = load_grants(user_id)
    # Role lookup only matters once an admin has limited something.
    roles = _roles_for(user_id) if grants else []
    return Access(grants, roles)


def may_use(user_id: str, connector: str, scope_id: str = "") -> bool:
    return access_for(user_id).may_use(connector, scope_id)


def credentials_for_user(user_id: str, connector: str, scope_id: str = "") -> Optional[frozenset]:
    return access_for(user_id).credentials(connector, scope_id)
