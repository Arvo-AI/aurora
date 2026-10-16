"""Per-platform teammate policy memories (today: ``context/Slack``).

Each chat platform Aurora acts on as a teammate gets one editable memory entry
holding its operating policy: tone, when to speak, the service -> channel
routing map, message format and per-channel notes. This module owns the spec
registry and the seeding that runs on connect. Each platform's wording lives in
its own module (``slack_memory``), and the entries' identity (category/title,
policy sources) lives in ``services.memory`` so the agent and the memory routes
can import it without pulling in the DB layer.

Add a platform by adding one spec here (built from that platform's module) and
one identity in ``services.memory.PLATFORM_MEMORY_IDENTITIES``; the full list of
registries a platform needs is in ``utils.notifications.team_routing``.

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
from services.memory.slack_memory import SLACK_MEMORY_DEFAULT_CONTENT, SLACK_MEMORY_DESCRIPTION
from services.memory.teams_memory import TEAMS_MEMORY_DEFAULT_CONTENT, TEAMS_MEMORY_DESCRIPTION

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


PLATFORM_MEMORY_SPECS: Dict[str, PlatformMemorySpec] = {
    "slack": PlatformMemorySpec(
        identity=PLATFORM_MEMORY_IDENTITIES["slack"],
        description=SLACK_MEMORY_DESCRIPTION,
        default_content=SLACK_MEMORY_DEFAULT_CONTENT,
    ),
    "teams": PlatformMemorySpec(
        identity=PLATFORM_MEMORY_IDENTITIES["teams"],
        description=TEAMS_MEMORY_DESCRIPTION,
        default_content=TEAMS_MEMORY_DEFAULT_CONTENT,
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
