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


def is_sharepoint_enabled() -> bool:
    """Check if SharePoint integration is enabled via environment variable."""
    return os.getenv("NEXT_PUBLIC_ENABLE_SHAREPOINT", "false").lower() == "true"


def is_spinnaker_enabled() -> bool:
    """Check if Spinnaker integration is enabled via environment variable."""
    return os.getenv("NEXT_PUBLIC_ENABLE_SPINNAKER", "false").lower() == "true"


def is_incident_prevention_enabled() -> bool:
    """Check if PR change-gating (Incident Prevention) is enabled."""
    return os.getenv("NEXT_PUBLIC_ENABLE_INCIDENT_PREVENTION", "true").lower() == "true"


def is_aurora_watch_enabled() -> bool:
    """Check if Aurora Watch may run on this installation.

    It is still off for every org until an admin turns it on; this is the
    operator's switch to make it unavailable altogether.
    """
    return os.getenv("NEXT_PUBLIC_ENABLE_AURORA_WATCH", "true").lower() == "true"
