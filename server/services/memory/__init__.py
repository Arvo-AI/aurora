MEMORY_CATEGORIES = ("context", "runbook", "infrastructure", "learned", "postmortem", "artifact")

# The "artifact" category is system-maintained: Aurora writes and grooms these
# entries (e.g. the Incident Index) itself. Users can view them but must not
# create/edit/recategorize/delete them, or the id-keyed lookups in
# services/memory break. Every other category is user-writable.
SYSTEM_CATEGORY = "artifact"
USER_WRITABLE_CATEGORIES = tuple(c for c in MEMORY_CATEGORIES if c != SYSTEM_CATEGORY)

# Agent-only categories — not exposed via user-facing memory routes.
AGENT_CATEGORIES = ()

# The single Incident Index artifact (one per org), identified by its title.
INCIDENT_INDEX_CATEGORY = SYSTEM_CATEGORY
INCIDENT_INDEX_TITLE = "Incident Index"

# All valid categories (used by memory_tool for validation)
ALL_CATEGORIES = MEMORY_CATEGORIES + AGENT_CATEGORIES
