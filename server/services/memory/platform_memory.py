"""Per-platform teammate policy memories (today: ``context/Slack``).

Each chat platform Aurora acts on as a teammate gets one editable memory entry
holding its operating policy: tone, when to speak, the service -> channel
routing map, message format and per-channel notes. This module owns the
default content of those entries and the seeding that runs on connect; their
identity (category/title, policy sources) lives in ``services.memory`` so the
agent and the memory routes can import it without pulling in the DB layer.

Contract: Slack rendering must stay byte-identical to the original
``slack_memory`` module. Add a platform by adding one spec here (and one
identity in ``services.memory.PLATFORM_MEMORY_IDENTITIES``); the
platform-specific modules (``slack_memory``) are thin shims over this one so
existing imports keep working.

Seeding only creates the starting policy and never overwrites an existing one.
"""

import logging
from dataclasses import dataclass
from typing import Dict, Optional

from utils.db.connection_pool import db_pool
from utils.auth.stateless_auth import set_rls_context
from utils.log_sanitizer import sanitize
from services.artifacts.store import create_version
from services.memory import PLATFORM_MEMORY_IDENTITIES, PlatformMemoryIdentity

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlatformMemorySpec:
    """Identity + default content of one platform's policy memory."""

    identity: PlatformMemoryIdentity
    description: str
    default_content: str

    @property
    def platform(self) -> str:
        return self.identity.platform

    @property
    def category(self) -> str:
        return self.identity.category

    @property
    def title(self) -> str:
        return self.identity.title


# ---------------------------------------------------------------------------
# Slack — strings moved verbatim from the original slack_memory module.
# ---------------------------------------------------------------------------

_SLACK_DESCRIPTION = (
    "Slack behaviour: tone, when Aurora speaks, and which teams/channels to "
    "notify. Aurora reads and updates this whenever Slack is involved."
)

# Default teammate policy — a conservative starting point users/agent refine over time.
_SLACK_DEFAULT_CONTENT = """\
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


PLATFORM_MEMORY_SPECS: Dict[str, PlatformMemorySpec] = {
    "slack": PlatformMemorySpec(
        identity=PLATFORM_MEMORY_IDENTITIES["slack"],
        description=_SLACK_DESCRIPTION,
        default_content=_SLACK_DEFAULT_CONTENT,
    ),
}


def get_spec(platform: str) -> Optional[PlatformMemorySpec]:
    return PLATFORM_MEMORY_SPECS.get((platform or "").lower())


def seed_platform_memory(user_id: str, platform: str, org_id: str | None = None) -> bool:
    """Create the default policy memory for an org if absent (idempotent,
    non-destructive). Returns True if a new entry was created.

    ``org_id`` should be passed by request handlers, which resolve it from the
    request (``get_org_id_from_request``) so the seed lands in the same org the
    caller reads back. Omitting it falls back to ``users.org_id`` — correct for
    background/OAuth callers with no request context, but that lookup is
    TTL-cached and can lag an org reassignment, seeding one org while the caller
    reads another.
    """
    spec = get_spec(platform)
    if not user_id or not spec:
        return False
    log = f"[PlatformMemory:{spec.platform}]"

    try:
        with db_pool.get_admin_connection() as conn, conn.cursor() as cursor:
            # One SET, from the org we're about to write. The INSERT must run
            # with myapp.current_org_id matching its org_id or FORCE ROW LEVEL
            # SECURITY rejects the row.
            org_id = set_rls_context(
                cursor, conn, user_id, org_id=org_id, log_prefix=f"[PlatformMemory:{spec.platform}:seed]"
            )
            if not org_id:
                logger.warning(
                    "%s No org for user; cannot seed %s memory", log, spec.title
                )
                return False

            # Already present — never overwrite user/agent edits on reconnect.
            cursor.execute(
                """SELECT id FROM artifacts
                   WHERE org_id = %s AND category = %s AND title = %s""",
                (org_id, spec.category, spec.title),
            )
            if cursor.fetchone():
                logger.info("%s %s memory already exists; leaving as-is", log, spec.title)
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
                    spec.title,
                    spec.default_content,
                    spec.category,
                    spec.description,
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
                spec.default_content,
                source="agent",
            )
            conn.commit()
            # org_id can arrive from a request header (X-Org-ID), so strip
            # control chars before logging — a newline would let a caller
            # forge log lines (S5145).
            logger.info(
                "%s Seeded default %s memory for org %s",
                log,
                spec.title,
                sanitize(org_id),
            )
            return True
    except Exception:
        logger.exception("%s Failed to seed %s memory", log, spec.title)
        return False
