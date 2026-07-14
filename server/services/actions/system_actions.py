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

from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

DEFAULT_MEMORY_CONSOLIDATION_INSTRUCTIONS = """You are a memory maintenance agent. Review and consolidate the org memory bank.

Use your memory tools (list_memories, read_memory, write_memory, edit_memory, append_to_memory, delete_memory) to:
1. Review what exists (list_memories + read_memory on entries that look duplicated or stale)
2. Merge duplicates (write the merged content to the better entry, delete the other)
3. Rewrite entries that need cleanup (edit_memory for surgical fixes, write_memory with overwrite for full rewrites)
4. Delete entries that are clearly stale or fully subsumed by another

GOALS:
- MERGE duplicates — if two entries cover the same topic, combine into one
- REMOVE stale entries — facts clearly outdated or contradicted by newer entries
- FIX formatting — ensure entries follow consistent structure
- DEDUPLICATE within entries — remove repeated paragraphs within a single entry
- CONVERT relative dates — "yesterday", "last week" → absolute dates where context allows

RULES:
- Be CONSERVATIVE — only act when confident the change improves things
- NEVER delete entries with unique, non-redundant information
- ALWAYS prefer merging over deleting
- Preserve all factual content during merges — don't lose information
- If unsure, leave the entry alone
- Use the updated_at timestamps from list_memories to judge staleness — bias toward keeping recently modified entries over older conflicting ones
- NEVER merge or delete postmortem entries — each one documents a unique incident. Only fix formatting within them.

If the memory bank looks clean, just respond "DONE: no changes needed" without making any modifications."""

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
        "system_key": "memory_consolidation",
        "name": "Memory Consolidation",
        "description": "Nightly review of org memory bank: merges duplicates, removes stale entries, fixes formatting, and ensures the memory index stays lean and accurate.",
        "trigger_type": "on_schedule",
        "trigger_config": {"interval_seconds": 86400},
        "mode": "agent",
        "instructions": None,
    },
]


def _get_default_instructions(system_key: str) -> str:
    """Resolve the default instructions for a given system action."""
    if system_key == "generate_postmortem":
        return DEFAULT_POSTMORTEM_INSTRUCTIONS
    if system_key == "alert_gap_audit":
        return DEFAULT_ALERT_GAP_INSTRUCTIONS
    if system_key == "memory_consolidation":
        return DEFAULT_MEMORY_CONSOLIDATION_INSTRUCTIONS
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
