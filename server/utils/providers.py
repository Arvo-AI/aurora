"""Canonical provider identifiers used across the codebase.

Keep this in sync with:
- connector blueprint directories under ``server/routes/`` (CONNECTOR_DIRS)
- the ``provider`` column of the ``user_tokens`` / ``user_connections`` tables
  (KNOWN_PROVIDERS — superset)
"""

__all__ = ["CONNECTOR_DIRS", "KNOWN_PROVIDERS"]

CONNECTOR_DIRS: frozenset = frozenset({
    "aws",
    "atlassian",
    "azure",
    "bigpanda",
    "bitbucket",
    "cloudbees",
    "cloudflare",
    "confluence",
    "coroot",
    "datadog",
    "dynatrace",
    "gcp",
    "github",
    "google_chat",
    "grafana",
    "incidentio",
    "jenkins",
    "jira",
    "netdata",
    "newrelic",
    "notion",
    "opsgenie",
    "ovh",
    "pagerduty",
    "scaleway",
    "sharepoint",
    "slack",
    "spinnaker",
    "splunk",
    "tailscale",
    "terraform",
    "thousandeyes",
})

# Auxiliary provider keys not backed by a routes/ directory but that do appear
# in the ``provider`` column (workspace selection, short-lived tokens, MCP).
_AUXILIARY_PROVIDERS: frozenset = frozenset({
    "bitbucket_workspace_selection",
    "jsm_ops",
    "kubectl",
    "mcp",
})

KNOWN_PROVIDERS: frozenset = CONNECTOR_DIRS | _AUXILIARY_PROVIDERS
