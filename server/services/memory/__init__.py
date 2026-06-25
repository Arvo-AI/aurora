MEMORY_CATEGORIES = ("context", "runbook", "infrastructure", "learned", "postmortem")

# Agent-only categories — not exposed via user-facing memory routes
AGENT_CATEGORIES = ("artifact",)

# All valid categories (used by memory_tool for validation)
ALL_CATEGORIES = MEMORY_CATEGORIES + AGENT_CATEGORIES
