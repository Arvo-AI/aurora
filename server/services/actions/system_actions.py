"""
System Actions Seeding

Ensures built-in system actions exist for each org.
Called on first login / org creation.
"""

import json
import logging
from typing import Optional

from services.actions.postmortem_action import DEFAULT_POSTMORTEM_INSTRUCTIONS
from services.actions.alert_gap_action import DEFAULT_ALERT_GAP_INSTRUCTIONS
from services.actions.hpa_vpa_action import DEFAULT_HPA_VPA_INSTRUCTIONS

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
        "instructions": None,
    },
    {
        "system_key": "alert_gap_audit",
        "name": "Alert Gap Audit",
        "description": "Periodically audits your infrastructure for alerting gaps and opens PRs/MRs with well-crafted alert definitions following SRE best practices.",
        "trigger_type": "on_schedule",
        "trigger_config": {"interval_seconds": 604800},
        "mode": "agent",
        "enabled": False,
        "instructions": None,
    },
    {
        "system_key": "hpa_vpa_rightsizing",
        # This name becomes the living-document artifact title, the chat session
        # title, and the text in Slack action notifications -- so it reads as a
        # document title and pairs with its sibling "Alert Gap Audit". "HPA/VPA"
        # is avoided deliberately: the slash reads badly as a title, and VPA is
        # not actually deployed in the target environment.
        "name": "Right-Sizing Audit",
        "description": "Periodically compares real CPU and memory usage against configured "
                       "requests, limits, and autoscaler bounds, and opens a PR per materially "
                       "mis-sized workload with a Slack card to review or dismiss.",
        "trigger_type": "on_schedule",
        "trigger_config": {"interval_seconds": 604800},
        # mode 'agent' is required, not stylistic: ModeAccessController strips
        # mcp_* tools in ask mode, which would kill PR creation outright.
        "mode": "agent",
        "enabled": False,
        "instructions": None,
    },
]


_DEFAULT_INSTRUCTIONS = {
    "generate_postmortem": DEFAULT_POSTMORTEM_INSTRUCTIONS,
    "alert_gap_audit": DEFAULT_ALERT_GAP_INSTRUCTIONS,
    "hpa_vpa_rightsizing": DEFAULT_HPA_VPA_INSTRUCTIONS,
}

# Fail fast at import if a SYSTEM_ACTIONS entry has no instructions. Deferring
# this to seeding time is far worse: _get_default_instructions runs inside the
# loop, so one unmapped key raises on every pass, gets swallowed by the except
# in seed_system_actions, and silently stops ALL system actions from seeding for
# every org that lacks them.
assert set(_DEFAULT_INSTRUCTIONS) >= {a["system_key"] for a in SYSTEM_ACTIONS}, (
    "SYSTEM_ACTIONS entries missing default instructions: "
    f"{ {a['system_key'] for a in SYSTEM_ACTIONS} - set(_DEFAULT_INSTRUCTIONS) }"
)


def _get_default_instructions(system_key: str) -> str:
    """Resolve the default instructions for a given system action."""
    try:
        return _DEFAULT_INSTRUCTIONS[system_key]
    except KeyError:
        raise ValueError(f"Unknown system action: {system_key}") from None


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
                    try:
                        instructions = _get_default_instructions(key)
                    except ValueError:
                        # Skip only the broken entry. Letting this propagate would
                        # abort the whole loop and leave the remaining system
                        # actions unseeded for this org.
                        logger.exception("[SystemActions] No default instructions for '%s'; skipping", key)
                        continue

                    cur.execute(
                        "SELECT id FROM actions WHERE org_id = %s AND system_key = %s",
                        (org_id, key),
                    )
                    if cur.fetchone():
                        continue

                    enabled = action_def.get("enabled", True)
                    cur.execute(
                        """INSERT INTO actions
                           (org_id, created_by, name, description, instructions,
                            trigger_type, trigger_config, mode, enabled,
                            is_system, system_key, default_instructions)
                           VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, true, %s, %s)""",
                        (
                            org_id,
                            creator,
                            action_def["name"],
                            action_def["description"],
                            instructions,
                            action_def["trigger_type"],
                            json.dumps(action_def["trigger_config"]),
                            action_def["mode"],
                            enabled,
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
