"""
Org-scoped SQL predicate helpers.

Provides two public utilities used wherever a database query must match rows
belonging to the requesting user OR any row shared across their org:

    resolve_org(user_id)  →  org_id string or None
    org_read_predicate(user_id, org_id)  →  (sql_fragment, params_tuple)

Security note
-------------
``org_read_predicate`` validates both IDs as well-formed UUIDs before they
are placed into query parameters.  This breaks the SQL-injection taint chain
for static analysis tools (e.g. CodeQL) even though psycopg2 parameterized
queries already prevent execution-level injection.  Do NOT bypass this
validation by calling ``_validate_uuid`` directly and constructing your own
predicate — always go through ``org_read_predicate``.
"""

import uuid as _uuid
from typing import Optional, Tuple


def _validate_uuid(value: str, label: str) -> str:
    """Return the canonical UUID string for *value*, raising ValueError if malformed."""
    try:
        return str(_uuid.UUID(str(value)))
    except (ValueError, AttributeError):
        raise ValueError(f"Invalid {label} format") from None


def resolve_org(user_id: str) -> Optional[str]:
    """Best-effort org_id resolution for use in database queries.

    Wraps ``stateless_auth.resolve_org_id`` and swallows all exceptions so
    that callers can safely fall back to user-only scoping when org resolution
    is unavailable (e.g. during Celery tasks before RLS context is set).
    """
    try:
        from utils.auth.stateless_auth import resolve_org_id
        return resolve_org_id(user_id)
    except Exception:
        return None


def org_read_predicate(user_id: str, org_id: Optional[str]) -> Tuple[str, Tuple]:
    """Build a SQL WHERE predicate that matches the requesting user OR their org.

    Both *user_id* and *org_id* are validated as well-formed UUIDs before
    being placed into the returned parameter tuple, breaking the taint chain
    from user-supplied input.

    Returns a ``(sql_fragment, params)`` pair for direct use in a WHERE
    clause::

        predicate, params = org_read_predicate(user_id, org_id)
        cursor.execute(f"SELECT ... FROM t WHERE {predicate} AND ...", params + (...,))

    When *org_id* is ``None`` or empty the predicate narrows to
    ``user_id = %s`` only (single-param tuple).
    """
    safe_user_id = _validate_uuid(user_id, "user_id")
    if org_id:
        safe_org_id = _validate_uuid(org_id, "org_id")
        return "(user_id = %s OR org_id = %s)", (safe_user_id, safe_org_id)
    return "user_id = %s", (safe_user_id,)
