"""
LangChain tool: load_skill

Allows the agent to load detailed integration guidance on-demand,
rather than having all guidance always present in the system prompt.
"""

import json
import logging
import threading
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Per-session tracking of already-loaded skills to avoid duplicate context
_loaded_skills: dict[str, set[str]] = {}
_loaded_skills_lock = threading.Lock()


def _session_key(user_id: str, session_id: str | None) -> str:
    return f"{user_id}:{session_id or 'default'}"


def clear_session_skills(user_id: str, session_id: str | None = None) -> None:
    """Clear loaded-skill tracking for a session (call on session end/reset)."""
    key = _session_key(user_id, session_id)
    with _loaded_skills_lock:
        _loaded_skills.pop(key, None)


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
    session_id = kwargs.get("session_id")
    if not user_id:
        logger.warning("load_skill called without user_id — with_user_context wrapper may have failed")
        return json.dumps({"error": "No user context available — this indicates a system configuration issue."})

    key = _session_key(user_id, session_id)

    try:
        from .registry import SkillRegistry

        registry = SkillRegistry.get_instance()

        # Fuzzy match: if exact ID not found, try normalizing and partial match
        if skill_id not in registry.get_all_skill_ids():
            normalized = skill_id.lower().replace("dot", ".").replace(".", "").replace("-", "").replace("_", "").replace(" ", "")
            best_match = None
            for candidate in sorted(registry.get_all_skill_ids()):
                candidate_norm = candidate.lower().replace(".", "").replace("-", "").replace("_", "")
                if normalized == candidate_norm:
                    best_match = candidate
                    break
                if (
                    len(normalized) >= 4
                    and len(candidate_norm) >= 4
                    and (candidate_norm.startswith(normalized) or normalized.startswith(candidate_norm))
                    and best_match is None
                ):
                    best_match = candidate
            if best_match:
                logger.info("load_skill fuzzy matched '%s' -> '%s'", skill_id, best_match)
                skill_id = best_match

        # Dedup after canonicalization so aliases don't bypass cache
        with _loaded_skills_lock:
            already_loaded = _loaded_skills.get(key, set())
            if skill_id in already_loaded:
                return f"Skill '{skill_id}' is already loaded in this conversation — no need to reload."

        result = registry.load_skill(skill_id, user_id)

        if not result.is_connected:
            all_ids = registry.get_all_skill_ids()
            if skill_id not in all_ids:
                connected = registry.get_connected_skill_ids(user_id)
                hint = f"Only these integrations have skills: {', '.join(connected)}. Cloud providers (aws, gcp, azure) use cloud_exec directly — no load_skill needed."
                return json.dumps({
                    "error": f"No skill '{skill_id}'. {hint}",
                    "available_skills": connected,
                })
            return json.dumps({
                "error": f"Integration '{skill_id}' is not connected for this user.",
                "hint": "Check the CONNECTED INTEGRATIONS index for available skills.",
                "available": registry.get_connected_skill_ids(user_id),
            })

        # Mark as loaded for this session
        with _loaded_skills_lock:
            if key not in _loaded_skills:
                _loaded_skills[key] = set()
            _loaded_skills[key].add(skill_id)

        return result.content

    except Exception:
        logger.exception("Error loading skill '%s'", skill_id)
        return json.dumps({"error": "Failed to load skill. Please retry or contact support if the issue persists."})
