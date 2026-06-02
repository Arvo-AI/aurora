"""
System Actions Seeding

Ensures built-in system actions exist for each org.
Called on first login / org creation.
"""

import json
import logging
from typing import Optional

from services.actions.postmortem_action import DEFAULT_POSTMORTEM_INSTRUCTIONS

from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

SYSTEM_ACTIONS = [
    {
        "system_key": "generate_postmortem",
        "name": "Generate Postmortem",
        "description": "Automatically generates a structured postmortem when an incident is resolved. Uses RCA data and connected communication tools (Slack) to gather context.",
        "trigger_type": "on_incident",
        "trigger_config": {"timing": "resolved"},
        "mode": "agent",
        "instructions": None,  # filled from postmortem_action.DEFAULT_POSTMORTEM_INSTRUCTIONS
    },
]


def _get_default_instructions(system_key: str) -> str:
    """Resolve the default instructions for a given system action."""
    if system_key == "generate_postmortem":
        return DEFAULT_POSTMORTEM_INSTRUCTIONS
    raise ValueError(f"Unknown system action: {system_key}")


def seed_system_actions(org_id: str, user_id: Optional[str] = None) -> int:
    """Ensure all system actions exist for an org.

    Returns the number of actions newly created.
    """
    created = 0
    creator = user_id or "system"

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                for action_def in SYSTEM_ACTIONS:
                    key = action_def["system_key"]
                    instructions = _get_default_instructions(key)

                    cur.execute(
                        "SELECT id FROM actions WHERE org_id = %s AND system_key = %s",
                        (org_id, key),
                    )
                    if cur.fetchone():
                        continue

                    cur.execute(
                        """INSERT INTO actions
                           (org_id, created_by, name, description, instructions,
                            trigger_type, trigger_config, mode, enabled,
                            is_system, system_key, default_instructions)
                           VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, true, true, %s, %s)""",
                        (
                            org_id,
                            creator,
                            action_def["name"],
                            action_def["description"],
                            instructions,
                            action_def["trigger_type"],
                            json.dumps(action_def["trigger_config"]),
                            action_def["mode"],
                            key,
                            instructions,
                        ),
                    )
                    created += 1
                    logger.info("[SystemActions] Seeded '%s' for org %s", key, org_id)

            conn.commit()
    except Exception:
        logger.exception("[SystemActions] Failed to seed actions for org %s", org_id)

    return created
