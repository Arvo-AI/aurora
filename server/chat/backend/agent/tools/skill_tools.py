"""Agent tools for loading skills and reading skill reference files."""

import logging
from typing import Optional
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class LoadSkillArgs(BaseModel):
    skill_name: str = Field(description="Name of the skill to load from the skills catalog")


class ReadSkillResourceArgs(BaseModel):
    skill_name: str = Field(description="Name of the skill")
    filename: str = Field(description="Reference file to read from the skill's resources")


LOAD_SKILL_DESCRIPTION = (
    "Load a skill's full procedural instructions by name. Use this when you need "
    "step-by-step guidance for a specific workflow listed in the skills catalog. "
    "Returns the full markdown body of the skill."
)

READ_SKILL_RESOURCE_DESCRIPTION = (
    "Read a reference file attached to a skill. Some skills include supplementary "
    "data files (templates, checklists, examples). Use this to retrieve them."
)


def _resolve_org_id(user_id: str, org_id: Optional[str] = None) -> str:
    if org_id:
        return org_id
    from utils.auth.stateless_auth import get_org_id_for_user
    return get_org_id_for_user(user_id) or ""


def load_skill(
    skill_name: str, user_id: Optional[str] = None,
    session_id: Optional[str] = None, org_id: Optional[str] = None, **kwargs
) -> str:
    if not user_id:
        return "Error: user_id is required to load skills."

    try:
        from chat.backend.agent.skills.skill_store import SkillStore

        resolved_org = _resolve_org_id(user_id, org_id)
        body = SkillStore.get_instance().get_skill_body(skill_name, user_id, resolved_org)

        if body is None:
            return f"Skill '{skill_name}' not found or not accessible."

        logger.info(f"Loaded skill '{skill_name}' for user {user_id}")
        return body
    except Exception as e:
        logger.exception(f"Error loading skill '{skill_name}': {e}")
        return f"Error loading skill '{skill_name}': {str(e)}"


def read_skill_resource(
    skill_name: str, filename: str, user_id: Optional[str] = None,
    session_id: Optional[str] = None, org_id: Optional[str] = None, **kwargs
) -> str:
    if not user_id:
        return "Error: user_id is required to read skill resources."

    try:
        from chat.backend.agent.skills.skill_store import SkillStore

        resolved_org = _resolve_org_id(user_id, org_id)
        content = SkillStore.get_instance().get_skill_resource(
            skill_name, filename, user_id, resolved_org
        )

        if content is None:
            return f"Resource '{filename}' not found in skill '{skill_name}'."

        logger.info(f"Read resource '{filename}' from skill '{skill_name}' for user {user_id}")
        return content
    except Exception as e:
        logger.exception(f"Error reading resource '{filename}' from skill '{skill_name}': {e}")
        return f"Error reading resource: {str(e)}"
