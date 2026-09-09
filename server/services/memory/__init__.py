MEMORY_CATEGORIES = ("context", "runbook", "infrastructure", "learned", "postmortem")

# Agent-only categories — not exposed via user-facing memory routes.
# incident_index: the org's single self-maintaining Incident Index artifact — a
# compact, deduplicated map of recent/relevant incidents (one line per incident,
# keyed by incident_id) that the recurrence agent scans to find candidate
# anchors before drilling into a specific incident. Kept out of MEMORY_CATEGORIES
# so it never appears in the user-facing memory routes or the injected user
# memory index; still writable/readable by background agents via ALL_CATEGORIES.
AGENT_CATEGORIES = ("artifact", "incident_index")

# Canonical identity of the single Incident Index artifact (one per org).
INCIDENT_INDEX_CATEGORY = "incident_index"
INCIDENT_INDEX_TITLE = "Incident Index"

# All valid categories (used by memory_tool for validation)
ALL_CATEGORIES = MEMORY_CATEGORIES + AGENT_CATEGORIES
