"""Filter parent-only state keys before crossing into a sub-agent asyncio.Task."""

# Keys that contain parent-session reasoning or large message history.
# Sub-agents must NOT receive these — they get only the bounded input dict
# delivered via SubAgentInput, not the parent's entire conversation.
_EXCLUDED_STATE_KEYS = frozenset({
    "messages",
    "parent_thoughts",
    "tool_call_history",
    "summarized_context",
    "triage_rationale",
    "synthesis_notes",
    "rca_ui_updates",
})


def filter_for_subagent(state_dict: dict) -> dict:
    """Strip parent-only fields before crossing into a sub-agent task body."""
    return {k: v for k, v in state_dict.items() if k not in _EXCLUDED_STATE_KEYS}
