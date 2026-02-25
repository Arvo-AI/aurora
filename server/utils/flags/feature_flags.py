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


def is_slack_enabled() -> bool:
    """Check if Slack integration is enabled via environment variable."""
    return os.getenv("NEXT_PUBLIC_ENABLE_SLACK", "false").lower() == "true"


def is_confluence_enabled() -> bool:
    """Check if Confluence integration is enabled via environment variable."""
    return os.getenv("NEXT_PUBLIC_ENABLE_CONFLUENCE", "false").lower() == "true"


def is_dynatrace_enabled() -> bool:
    """Check if Dynatrace integration is enabled via environment variable."""
    return os.getenv("NEXT_PUBLIC_ENABLE_DYNATRACE", "false").lower() == "true"
