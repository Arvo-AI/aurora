MEMORY_CATEGORIES = ("context", "runbook", "infrastructure", "learned", "postmortem", "artifact")

# Agent-only categories — not exposed via user-facing memory routes.
AGENT_CATEGORIES = ()

# Canonical identity of the single Incident Index artifact (one per org).
# It lives in the shared, agent-maintained "artifact" category (same as the
# scheduled-action living documents): visible read-only in the user memory UI
# (excluded from USER_WRITABLE_CATEGORIES on the client) and discoverable by
# agents via list_memories. Identified by its exact title.
INCIDENT_INDEX_CATEGORY = "artifact"
INCIDENT_INDEX_TITLE = "Incident Index"

# All valid categories (used by memory_tool for validation)
ALL_CATEGORIES = MEMORY_CATEGORIES + AGENT_CATEGORIES
