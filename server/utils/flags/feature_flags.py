"""Feature flags for toggling functionality.
Uses NEXT_PUBLIC_ENABLE_* variables shared with frontend for single source of truth.
"""
import os

def is_ovh_enabled() -> bool:
    """Check if OVH integration is enabled via environment variable."""
    return os.getenv("NEXT_PUBLIC_ENABLE_OVH", "false").lower() == "true"


def is_pagerduty_oauth_enabled() -> bool:
    """Check if PagerDuty OAuth integration is enabled via environment variable."""
    return os.getenv("NEXT_PUBLIC_ENABLE_PAGERDUTY_OAUTH", "false").lower() == "true"


def is_confluence_enabled() -> bool:
    """Confluence is always enabled (no longer feature-gated)."""
    return True


def is_jira_enabled() -> bool:
    """Check if Jira integration is enabled via environment variable."""
    return os.getenv("NEXT_PUBLIC_ENABLE_JIRA", "false").lower() == "true"


def is_sharepoint_enabled() -> bool:
    """Check if SharePoint integration is enabled via environment variable."""
    return os.getenv("NEXT_PUBLIC_ENABLE_SHAREPOINT", "false").lower() == "true"


def is_spinnaker_enabled() -> bool:
    """Check if Spinnaker integration is enabled via environment variable."""
    return os.getenv("NEXT_PUBLIC_ENABLE_SPINNAKER", "false").lower() == "true"
