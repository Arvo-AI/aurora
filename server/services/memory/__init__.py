MEMORY_CATEGORIES = ("context", "runbook", "infrastructure", "learned", "postmortem", "artifact")

# Agent-only categories — not exposed via user-facing memory routes.
AGENT_CATEGORIES = ()

# The single Incident Index artifact (one per org), identified by its title.
# Stored in the shared "artifact" category so it's discoverable via list_memories
# and read-only in the user memory UI — no dedicated category needed.
INCIDENT_INDEX_CATEGORY = "artifact"
INCIDENT_INDEX_TITLE = "Incident Index"

# All valid categories (used by memory_tool for validation)
ALL_CATEGORIES = MEMORY_CATEGORIES + AGENT_CATEGORIES
