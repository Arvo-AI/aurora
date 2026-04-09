"""Shared constants for the OpsGenie integration."""

MAX_OUTPUT_SIZE = 120_000  # ~120KB, matching other observability tools
MAX_RESULTS_CAP = 1000
OPSGENIE_TIMEOUT = 20

REGION_URLS = {
    "us": "https://api.opsgenie.com",
    "eu": "https://api.eu.opsgenie.com",
}
