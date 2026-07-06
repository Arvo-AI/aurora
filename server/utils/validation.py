"""Generic input validators shared across routes, tools, and tasks."""

import uuid


def is_valid_uuid(value) -> bool:
    """Return True if ``value`` is a well-formed UUID string."""
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False
