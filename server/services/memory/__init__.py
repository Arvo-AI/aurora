MEMORY_CATEGORIES = ("context", "runbook", "infrastructure", "learned", "postmortem", "artifact")

# The "artifact" category is system-maintained: Aurora writes and grooms these
# entries (e.g. the Incident Index) itself. Users can view them but must not
# create/edit/recategorize/delete them, or the id-keyed lookups in
# services/memory break. Every other category is user-writable.
SYSTEM_CATEGORY = "artifact"
USER_WRITABLE_CATEGORIES = tuple(c for c in MEMORY_CATEGORIES if c != SYSTEM_CATEGORY)

# Agent-only categories — not exposed via user-facing memory routes. Currently
# none: the Incident Index lives under the system "artifact" category above.
AGENT_CATEGORIES = ()

# The single Incident Index artifact (one per org), identified by its title.
INCIDENT_INDEX_CATEGORY = SYSTEM_CATEGORY
INCIDENT_INDEX_TITLE = "Incident Index"

# The Slack teammate-policy entry (one per org), seeded on Slack connect. Unlike
# the Incident Index this lives in a user-writable category on purpose — both the
# user and the agent edit its content — but its identity is protected below.
SLACK_MEMORY_CATEGORY = "context"
SLACK_MEMORY_TITLE = "Slack"

# Well-known entries whose (category, title) pair IS their stable identity —
# seeders, the agent's prompt injector, and route lookups all pin to it rather
# than to an id. Users may freely edit their content and description, but renaming,
# recategorizing, or deleting one would silently detach it from those lookups
# (e.g. the Slack policy would stop being injected, with no error), so the
# memory routes reject those operations.
PROTECTED_ENTRIES = frozenset({
    (SLACK_MEMORY_CATEGORY, SLACK_MEMORY_TITLE),
})

# All valid categories (used by memory_tool for validation)
ALL_CATEGORIES = MEMORY_CATEGORIES + AGENT_CATEGORIES
