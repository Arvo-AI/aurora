# Auth utilities package.

ROLE_ADMIN = "admin"
ROLE_EDITOR = "editor"
ROLE_VIEWER = "viewer"

VALID_ROLES = frozenset({ROLE_ADMIN, ROLE_EDITOR, ROLE_VIEWER})


def normalize_email(email) -> str:
    """Canonicalize an email for storage and lookup.

    Email addresses are effectively case-insensitive in practice, but mobile
    keyboards autocapitalize the first letter and password managers replay
    whatever case was first typed. Storing and comparing a single canonical
    form keeps an account reachable no matter how the user capitalizes it, and
    stops a capitalized and a lowercase spelling of one address from becoming
    two separate accounts.

    Lives here rather than in a route module so login, registration, admin
    user creation and GitHub provisioning can all share it without importing
    each other.
    """
    if not isinstance(email, str):
        return ""
    return email.strip().lower()
