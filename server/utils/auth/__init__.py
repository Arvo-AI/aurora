# Auth utilities package.

ROLE_ADMIN = "admin"
ROLE_EDITOR = "editor"
ROLE_VIEWER = "viewer"

VALID_ROLES = frozenset({ROLE_ADMIN, ROLE_EDITOR, ROLE_VIEWER})


def normalize_email(email) -> str:
    """Canonical email form for storage and lookup. Non-strings become "".

    The isinstance guard matters: callers pass data.get("email") straight in,
    which is None on a missing key and would raise on .strip().
    """
    if not isinstance(email, str):
        return ""
    return email.strip().lower()
