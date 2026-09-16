"""
Slack memory seeding.

Aurora keeps all Slack behavioural context (tone, when to speak, which teams
to notify, per-channel preferences) in a single, ordinary user-writable memory
entry titled "Slack" in the ``context`` category. Because it's a normal memory
entry it is automatically:

- injected into the agent's system prompt by ``services.memory.injector`` when
  relevant (e.g. any Slack-related message),
- visible and editable by humans in the Memory settings UI,
- editable by the agent via the standard ``edit_memory`` / ``append_to_memory``
  tools (so it can learn team preferences over time — e.g. "be quiet in this
  channel").

This module only *seeds* the entry on Slack connect so Aurora starts with a
sensible default teammate policy. It never overwrites an existing entry, so any
edits made by users or the agent survive reconnects.
"""

import logging

from utils.db.connection_pool import db_pool
from utils.auth.stateless_auth import set_rls_context
from services.artifacts.store import create_version

logger = logging.getLogger(__name__)

# Well-known identity of the Slack memory entry. Kept in one place so the
# injector, seeding, and any agent guidance all reference the same title.
SLACK_MEMORY_CATEGORY = "context"
SLACK_MEMORY_TITLE = "Slack"
SLACK_MEMORY_DESCRIPTION = (
    "Slack behaviour: tone, when Aurora speaks, and which teams/channels to "
    "notify. Aurora reads and updates this whenever Slack is involved."
)

# Default teammate policy. Deliberately concise-professional and conservative:
# post conclusions, otherwise stay quiet. Both users and the agent are expected
# to refine this over time; it is only the starting point.
SLACK_MEMORY_DEFAULT_CONTENT = """\
This is Aurora's operating policy for Slack. Aurora acts like a teammate here, \
not a notification bot. Update this entry as the team states preferences.

## Tone
- Concise and professional. Short, direct answers — no filler, no forced section \
headers, minimal formatting.
- Reply in the thread you were addressed in. Build on the existing conversation \
rather than repeating it.

## When to speak
- When an investigation reaches a conclusion, post it to the relevant channel.
- Otherwise stay quiet — don't narrate progress or post without something useful \
to say.
- Always respond when directly @mentioned.
- If a team asks Aurora to be quieter (or more verbose) in a channel, record that \
here per-channel and honour it.

## Which channels / teams to notify
- Aurora keeps a list of the Slack channels it can see, each with a description \
(see the get_slack_channels tool). Use those descriptions to pick the channel(s) \
relevant to a given incident, service, or team.
- Default fallback is the shared incidents channel when no better match exists.
- Record team → channel routing preferences here as they are learned.

## Per-channel notes
(none yet — Aurora and the team add channel-specific preferences here over time)
"""


def seed_slack_memory(user_id: str) -> bool:
    """Create the default "Slack" memory entry for the user's org if absent.

    Idempotent and non-destructive: if an entry already exists (seeded before,
    or edited by a human/agent) it is left untouched so preferences are never
    clobbered on reconnect. Returns True if a new entry was created.
    """
    if not user_id:
        return False

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                org_id = set_rls_context(
                    cursor, conn, user_id, log_prefix="[SlackMemory:seed]"
                )
                if not org_id:
                    logger.warning(
                        "[SlackMemory] No org for user; cannot seed Slack memory"
                    )
                    return False

                # Already present — never overwrite user/agent edits on reconnect.
                cursor.execute(
                    """SELECT id FROM artifacts
                       WHERE org_id = %s AND category = %s AND title = %s""",
                    (org_id, SLACK_MEMORY_CATEGORY, SLACK_MEMORY_TITLE),
                )
                if cursor.fetchone():
                    logger.info("[SlackMemory] Slack memory already exists; leaving as-is")
                    return False

                # Seed the default policy. last_edited_by='agent' since Aurora
                # created it; a human editing it in the UI flips it to 'user'.
                cursor.execute(
                    """INSERT INTO artifacts
                           (org_id, user_id, title, content, category, description,
                            last_edited_by, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s, 'agent', CURRENT_TIMESTAMP)
                       ON CONFLICT (org_id, category, title) DO NOTHING
                       RETURNING id""",
                    (
                        org_id,
                        user_id,
                        SLACK_MEMORY_TITLE,
                        SLACK_MEMORY_DEFAULT_CONTENT,
                        SLACK_MEMORY_CATEGORY,
                        SLACK_MEMORY_DESCRIPTION,
                    ),
                )
                row = cursor.fetchone()
                # A concurrent seed won the race (ON CONFLICT DO NOTHING) — fine.
                if not row:
                    conn.commit()
                    return False

                artifact_id = str(row[0])
                create_version(
                    cursor,
                    artifact_id,
                    org_id,
                    user_id,
                    SLACK_MEMORY_DEFAULT_CONTENT,
                    source="agent",
                )
                conn.commit()
                logger.info("[SlackMemory] Seeded default Slack memory for org %s", org_id)
                return True
    except Exception:
        logger.exception("[SlackMemory] Failed to seed Slack memory")
        return False
