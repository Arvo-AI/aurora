"""
Infrastructure Context Tool

Save and retrieve the consolidated infrastructure context document.
The prediscovery agent writes this; internal and MCP agents read it.
"""

import logging

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class SaveInfraContextArgs(BaseModel):
    content: str = Field(
        description=(
            "The full consolidated infrastructure context document. "
            "Covers environments, services, dependencies, CI/CD, monitoring, "
            "and how they interconnect."
        )
    )


class GetInfraContextArgs(BaseModel):
    pass


SAVE_INFRA_CONTEXT_DESCRIPTION = (
    "Save the consolidated infrastructure context document. Call this ONCE at the "
    "end of your investigation with the complete synthesized document covering all "
    "environments, services, dependencies, CI/CD pipelines, and monitoring."
)

GET_INFRA_CONTEXT_DESCRIPTION = (
    "Retrieve the infrastructure context document for this organization. Contains "
    "environments, services, dependencies, CI/CD pipelines, and monitoring topology. "
    "Call when you need to understand system architecture or service relationships."
)


def save_infrastructure_context(
    content: str,
    user_id: str | None = None,
    **kwargs,
) -> str:
    """Upsert the infrastructure context document for the user's org."""
    if not user_id:
        return "Error: User authentication required."
    if not content or len(content.strip()) < 100:
        return "Error: Content too short. Provide a comprehensive infrastructure document."
    if len(content.strip()) > 100_000:
        return "Error: Content too large (max 100k characters)."

    try:
        from utils.auth.stateless_auth import get_org_id_for_user
        from utils.db.connection_pool import db_pool

        org_id = get_org_id_for_user(user_id)
        if not org_id:
            return "Error: No organization context."

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO infrastructure_context (org_id, content, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (org_id) DO UPDATE
                    SET content = EXCLUDED.content, updated_at = NOW()
                    """,
                    (org_id, content.strip()),
                )
            conn.commit()

        logger.info(f"[InfraContext] Saved infrastructure context for org {org_id} ({len(content)} chars)")
        return "Infrastructure context saved successfully."

    except Exception as e:
        logger.exception(f"[InfraContext] Error saving context: {e}")
        return f"Error saving infrastructure context: {str(e)}"


def get_infrastructure_context(
    user_id: str | None = None,
    **kwargs,
) -> str:
    """Retrieve the infrastructure context document for the user's org."""
    if not user_id:
        return "Error: User authentication required."

    try:
        from utils.auth.stateless_auth import get_org_id_for_user
        from utils.db.connection_pool import db_pool

        org_id = get_org_id_for_user(user_id)
        if not org_id:
            return "Error: No organization context."

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT content, updated_at FROM infrastructure_context WHERE org_id = %s",
                    (org_id,),
                )
                row = cur.fetchone()

        if not row:
            return "No infrastructure context available yet. Run prediscovery to generate it."

        content, updated_at = row
        return f"[Last updated: {updated_at.isoformat()}]\n\n{content}"

    except Exception as e:
        logger.exception(f"[InfraContext] Error retrieving context: {e}")
        return f"Error retrieving infrastructure context: {str(e)}"
