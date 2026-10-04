"""Slack shim over ``services.memory.platform_memory``.

The default "Slack" memory entry (category ``context``, title ``Slack``) is
seeded on connect. It's an ordinary memory entry — this only creates the
starting policy, never overwrites an existing one. All names exported here keep
their original values; the platform-generic implementation lives in
``platform_memory``.
"""

from services.memory import SLACK_MEMORY_CATEGORY, SLACK_MEMORY_TITLE  # noqa: F401
from services.memory import platform_memory as _pm

_SPEC = _pm.PLATFORM_MEMORY_SPECS["slack"]

SLACK_MEMORY_DESCRIPTION = _SPEC.description
SLACK_MEMORY_DEFAULT_CONTENT = _SPEC.default_content


def seed_slack_memory(user_id: str, org_id: str | None = None) -> bool:
    """Create the default "Slack" memory for an org if absent (idempotent,
    non-destructive). Returns True if a new entry was created. See
    ``seed_platform_memory`` for the ``org_id`` contract."""
    return _pm.seed_platform_memory(user_id, "slack", org_id=org_id)
