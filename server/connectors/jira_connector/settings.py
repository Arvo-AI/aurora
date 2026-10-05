"""How much Aurora is allowed to write back to Jira, per org.

Default is read-only. Atlassian 3LO has no bot principal, so anything Aurora
posts is authored by the Atlassian account that connected the integration —
writing has to be opt-in, not the default for a connector people add as a
context source.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

JIRA_MODE_KEY = "jira_mode"

# Search and read issues only — never posts.
READ_ONLY = "read_only"
# May also comment on issues that already exist.
COMMENT_ONLY = "comment_only"
# May also create, update and link issues.
FULL = "full"

VALID_MODES = (READ_ONLY, COMMENT_ONLY, FULL)
DEFAULT_MODE = READ_ONLY


def normalize_jira_mode(mode: Optional[str]) -> str:
    """Coerce a stored or client-supplied mode to a known value.

    Unknown values fail closed to read-only rather than granting write access.
    """
    candidate = (mode or "").strip().lower()
    if candidate in VALID_MODES:
        return candidate
    if candidate:
        logger.warning("[JIRA] Unknown jira_mode %r — falling back to %s", candidate, DEFAULT_MODE)
    return DEFAULT_MODE


def get_jira_mode(user_id: str) -> str:
    """Read the org's Jira mode preference (org-scoped via user_preferences)."""
    from utils.auth.stateless_auth import get_user_preference

    return normalize_jira_mode(get_user_preference(user_id, JIRA_MODE_KEY, default=DEFAULT_MODE))


def jira_writes_allowed(mode: Optional[str]) -> bool:
    """True when Aurora may post to Jira at all."""
    return normalize_jira_mode(mode) != READ_ONLY
