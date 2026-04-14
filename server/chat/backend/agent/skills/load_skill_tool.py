"""
LangChain tool: load_skill

Allows the agent to load detailed integration guidance on-demand,
rather than having all guidance always present in the system prompt.
"""

import json
import logging
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class LoadSkillArgs(BaseModel):
    """Arguments for the load_skill tool."""
    skill_id: str = Field(
        description=(
            "ID of the integration skill to load (from the CONNECTED INTEGRATIONS index). "
            "Examples: 'github', 'splunk', 'ovh', 'datadog', 'jenkins'."
        )
    )


def load_skill(skill_id: str, **kwargs) -> str:
    """
    Load detailed guidance for a connected integration.

    Call this when you need to use an integration's tools and want the full
    reference (commands, workflows, rules, investigation steps). The
    CONNECTED INTEGRATIONS index in your system prompt lists available skills.
    """
    user_id = kwargs.get("user_id")
    if not user_id:
        return json.dumps({"error": "No user context available"})

    try:
        from .registry import SkillRegistry

        registry = SkillRegistry.get_instance()
        result = registry.load_skill(skill_id, user_id)

        if not result.is_connected:
            return json.dumps({
                "error": f"Integration '{skill_id}' is not connected.",
                "hint": "Check the CONNECTED INTEGRATIONS index for available skills.",
                "available": registry.get_connected_skill_ids(user_id),
            })

        return result.content

    except Exception as e:
        logger.error(f"Error loading skill '{skill_id}': {e}", exc_info=True)
        return json.dumps({"error": f"Failed to load skill: {e}"})
