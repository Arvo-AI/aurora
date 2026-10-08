"""Offline channel classification shared by chat-platform channel registries.

The heuristics moved verbatim from routes.slack.slack_channels so every provider
classifies the same way; each platform extracts the name and descriptive text
from its own channel shape and passes them in. Kept free of Celery/LLM imports
so route modules can import it cheaply.
"""

import re
from typing import Optional, Tuple


# Word-boundary patterns for incident-platform detection. Using anchored regex
# (rather than a bare substring `in` check) both avoids matching a platform
# name embedded in an unrelated token and clears CodeQL's "incomplete URL
# substring sanitization" rule, which flags substring checks on URL-like text.
_PLATFORM_PATTERNS = (
    ("incident.io", re.compile(r"\bincident\.io\b|\bincidentio\b")),
    ("pagerduty", re.compile(r"\bpagerduty\b|\bpd-incident\b")),
    ("opsgenie", re.compile(r"\bopsgenie\b")),
)

# Channel-name heuristics. Anchored on the left (token start) so we match
# "incident"/"alert" as words/prefixes, not an arbitrary substring mid-token
# (also clears CodeQL's URL-substring sanitization rule). Right side is loose so
# "alerts"/"oncall-db"/"incident-42" still match.
_INCIDENT_NAME_RE = re.compile(r"(?:^|[\s\-_])inc(?:ident)?(?:[\s\-_]|$)|\bincident")
_TEAM_NAME_RE = re.compile(r"\balert|\bon-?call|\bsev(?:[\s\-_]|$)")


def classify_channel(name: str, haystack: str) -> Tuple[str, Optional[str]]:
    """Best-effort (channel_type, detected_platform) from a channel's lowercased
    ``name`` and ``haystack`` (name plus any descriptive text, lowercased).

    Generic heuristic (per product requirement: support any platform that
    creates channels, e.g. incident.io/PagerDuty/Opsgenie). The LLM description
    task refines this later; this is only the fast, offline first guess so the
    UI/agent have something immediately.
    """
    # Detect the incident-management platform that spawned the channel, if any.
    platform = None
    for platform_name, pattern in _PLATFORM_PATTERNS:
        if pattern.search(haystack):
            platform = platform_name
            break

    # Incident channels: platform-created OR named like one. Anchored patterns
    # (not bare `in name`) both read as intent and clear CodeQL's URL-substring
    # rule, which flags substring membership on URL-like text.
    if platform or _INCIDENT_NAME_RE.search(name):
        return "incident", platform
    # Alerting/on-call channels are still team-facing routing targets.
    if _TEAM_NAME_RE.search(name):
        return "team", platform
    return "general", platform
