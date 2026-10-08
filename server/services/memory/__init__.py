from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

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

# Per-platform teammate-policy entries (one per org per chat platform), seeded
# on connect. Unlike the Incident Index these live in a user-writable category on
# purpose — both the user and the agent edit their content — but their identity
# is protected below. Adding a platform means adding one entry to the registry;
# the Slack entry must keep rendering byte-identically.
@dataclass(frozen=True)
class PlatformMemoryIdentity:
    platform: str
    category: str
    title: str
    # Background-session sources that speak on this platform and must always
    # have its memory injected (not left to the LLM memory selector): the
    # @mention reply source. Do not list TEAM_ROUTING_SOURCE here — one post-RCA
    # team-routing agent decides for every connected platform in the same run,
    # so policy_entries_for_source gives it every registered platform's memory.
    policy_sources: frozenset

    @property
    def key(self) -> Tuple[str, str]:
        """(category, title) — the tuple ``MemoryPrefetch.force_entries`` takes."""
        return (self.category, self.title)


TEAM_ROUTING_SOURCE = "team_routing"

PLATFORM_MEMORY_IDENTITIES: Dict[str, PlatformMemoryIdentity] = {
    "slack": PlatformMemoryIdentity(
        platform="slack",
        category="context",
        title="Slack",
        policy_sources=frozenset({"slack"}),
    ),
    "teams": PlatformMemoryIdentity(
        platform="teams",
        category="context",
        title="Microsoft Teams",
        policy_sources=frozenset({"teams"}),
    ),
}

# Slack aliases — kept so existing imports keep working.
SLACK_MEMORY_CATEGORY = PLATFORM_MEMORY_IDENTITIES["slack"].category
SLACK_MEMORY_TITLE = PLATFORM_MEMORY_IDENTITIES["slack"].title

# Well-known entries whose (category, title) pair IS their stable identity —
# seeders, the agent's prompt injector, and route lookups all pin to it rather
# than to an id. Users may freely edit their content and description, but renaming,
# recategorizing, or deleting one would silently detach it from those lookups
# (e.g. the Slack policy would stop being injected, with no error), so the
# memory routes reject those operations.
PROTECTED_ENTRIES = frozenset(i.key for i in PLATFORM_MEMORY_IDENTITIES.values())


def policy_entries_for_source(source: Optional[str]) -> List[Tuple[str, str]]:
    """Memory entries to force-inject for a background session.

    ``source`` is the session's trigger source (``rca_context["source"]`` or the
    raw ``trigger_metadata["source"]``).

        slack         -> [("context", "Slack")]
        team_routing  -> every registered platform's entry: one routing agent
                         decides for all connected platforms in the same run
                         (the injector skips an entry that was never seeded)
        anything else -> []
    """
    src = (source or "").strip().lower()
    if not src:
        return []
    if src == TEAM_ROUTING_SOURCE:
        return [i.key for i in PLATFORM_MEMORY_IDENTITIES.values()]
    return [i.key for i in PLATFORM_MEMORY_IDENTITIES.values() if src in i.policy_sources]


# All valid categories (used by memory_tool for validation)
ALL_CATEGORIES = MEMORY_CATEGORIES + AGENT_CATEGORIES
