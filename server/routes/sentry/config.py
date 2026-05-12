"""Shared constants for the Sentry integration."""

MAX_QUERY_LENGTH = 2000
MAX_OUTPUT_SIZE = 2 * 1024 * 1024  # 2 MB
MAX_RESULTS_CAP = 100

VALID_ISSUE_RESOURCES = frozenset({
    "issues",
    "issue_detail",
    "issue_event",
    "projects",
    "events",
})

# Sentry webhook resource header values we accept.
VALID_WEBHOOK_RESOURCES = frozenset({
    "issue",
    "error",
    "event_alert",
    "installation",
    "comment",
})
