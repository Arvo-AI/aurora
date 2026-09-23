"""Seed the default "Slack" memory entry (category ``context``, title ``Slack``)
on connect. It's an ordinary memory entry — this only creates the starting policy, 
never overwrites an existing one.
"""

import logging

from utils.db.connection_pool import db_pool
from utils.auth.stateless_auth import set_rls_context
from services.artifacts.store import create_version
from services.memory.queries import get_memory_content

logger = logging.getLogger(__name__)

# Well-known identity of the Slack memory entry — referenced by seeding + injector.
SLACK_MEMORY_CATEGORY = "context"
SLACK_MEMORY_TITLE = "Slack"
SLACK_MEMORY_DESCRIPTION = (
    "Slack behaviour: tone, when Aurora speaks, and which teams/channels to "
    "notify. Aurora reads and updates this whenever Slack is involved."
)

# Default teammate policy — a conservative starting point users/agent refine over time.
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
(see the get_connected_slack_channels tool). Use those descriptions to pick the channel(s) \
relevant to a given incident, service, or team.
- Default fallback is the shared incidents channel when no better match exists.
- Record team → channel routing preferences here as they are learned.

## Service -> channel routing map
Aurora learns, over time, which team channel owns which service/component, and \
records it here so future incidents route straight to the right place without \
re-deriving it. When you conclude an incident and post to a channel because it \
owns the affected service, append the mapping here (e.g. "payments -> \
#payments-oncall", "checkout-api -> #team-checkout"). On a new incident, consult \
this map FIRST, before scanning channel descriptions. If a mapping turns out \
wrong (a team redirects you), correct it here.

(none yet — Aurora fills this in as it learns which channel owns which service)

## Message format
- Incident notifications are composed per channel. Some teams want a structured \
summary (alert, severity, service, root cause, link); others want a short, human \
one-liner. State the preference here — org-wide and/or per-channel.
- Default: a concise structured summary. Record any channel/team that prefers a \
different style under "Per-channel notes".

## Per-channel notes
(none yet — Aurora and the team add channel-specific preferences here over time)
"""


def read_slack_memory(user_id: str) -> str:
    """Return the org's current "Slack" memory content, or "" if none/error.

    Thin wrapper over the shared ``get_memory_content`` helper, pinned to the
    Slack entry's (category, title) and normalising its ``None`` to "" for the
    routing / non-agent callers that expect a string.
    """
    if not user_id:
        return ""
    return get_memory_content(user_id, SLACK_MEMORY_CATEGORY, SLACK_MEMORY_TITLE) or ""


def seed_slack_memory(user_id: str) -> bool:
    """Create the default "Slack" memory for the user's org if absent (idempotent,
    non-destructive). Returns True if a new entry was created."""
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
