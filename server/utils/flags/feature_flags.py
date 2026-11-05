"""Feature flags for toggling functionality.
Uses NEXT_PUBLIC_ENABLE_* variables shared with frontend for single source of truth.
"""
import os

def is_incident_prevention_enabled() -> bool:
    """Check if PR change-gating (Incident Prevention) is enabled."""
    return os.getenv("NEXT_PUBLIC_ENABLE_INCIDENT_PREVENTION", "true").lower() == "true"
