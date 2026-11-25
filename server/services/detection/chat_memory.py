"""Per-channel levels, kept in Aurora's Slack memory.

There is one control outside chat: the slider in Settings, which sets the level
of every public channel (services/detection/chat_signals.py). Anything more
specific is said to Aurora ("be quieter in #payments") and lives where the rest
of what a team tells Aurora about Slack lives: the ``context/Slack`` memory
entry, in a section a person can also read and edit:

    ## Dynamic alerting
    - #payments: listen
    - #sec-incident: off

One line per channel, one of the four level words. A line wins over the slider
for that channel, in either direction, except that a slider at off is off
everywhere. A private channel is read only if a line names it.

The lines are the configuration. ``detection_chat_cursors.level`` is a copy of
them, refreshed by ``sync`` wherever a level is about to be used.
"""

import logging
import re
from typing import Dict, Optional

from services.detection.chat_signals import LEVELS
from services.memory import SLACK_MEMORY_CATEGORY, SLACK_MEMORY_TITLE

logger = logging.getLogger(__name__)

SECTION = "## Dynamic alerting"
_INTRO = """\
How much Aurora does in a channel without being asked. The slider in Settings sets it for every \
public channel. A line here sets it for one channel, and Aurora adds or changes a line when \
someone asks it to be quieter or louder somewhere. One line per channel, as `- #channel: level`.
Levels, quietest first: `off` (Aurora does not read the channel), `listen` (reads it and looks \
into problems, never posts), `sure` (also replies in the thread once it has confirmed something \
is broken), `free` (also answers questions and says what it checked when it found nothing wrong).
A private channel is read only if it has a line here."""

_HEADING = re.compile(r"^##[ \t]+(\S[^\n]*)$", re.MULTILINE)
_LINE = re.compile(
    r"^\s*[-*]\s*`?#?(?P<name>[\w.-]{1,80})`?\s*:\s*`?(?P<level>%s)`?\s*$" % "|".join(LEVELS),
    re.IGNORECASE,
)


def _section_span(content: str) -> Optional[tuple]:
    """(start, end) of the section's body, or None when the entry has no such section."""
    for heading in _HEADING.finditer(content):
        if heading.group(1).strip().lower() == SECTION[3:].lower():
            following = _HEADING.search(content, heading.end())
            return heading.end(), following.start() if following else len(content)
    return None


def parse(content: str) -> Dict[str, str]:
    """Channel name (lowercase, no #) to level, from the section. A later line wins."""
    span = _section_span(content or "")
    if span is None:
        return {}
    levels = {}
    for line in content[span[0]:span[1]].splitlines():
        found = _LINE.match(line)
        if found:
            levels[found.group("name").lower()] = found.group("level").lower()
    return levels


def with_section(content: str) -> str:
    """*content* with the section present. Unchanged when it already is."""
    content = content or ""
    if _section_span(content) is not None:
        return content
    return content.rstrip("\n") + "\n\n" + SECTION + "\n" + _INTRO + "\n"


def with_level(content: str, channel_name: str, level: Optional[str]) -> str:
    """*content* with the channel's line set to *level*, or removed when it is None."""
    content = with_section(content)
    start, end = _section_span(content)
    name = channel_name.lstrip("#").lower()
    kept = [
        line for line in content[start:end].rstrip("\n").split("\n")
        if not ((found := _LINE.match(line)) and found.group("name").lower() == name)
    ]
    if level:
        kept.append("- #%s: %s" % (name, level))
    return content[:start] + "\n".join(kept) + "\n" + ("\n" if end < len(content) else "") + content[end:]


def _entry(cursor, org_id: str, lock: bool = False) -> Optional[tuple]:
    cursor.execute(
        "SELECT id, content FROM artifacts WHERE org_id = %s AND category = %s AND title = %s"
        + (" FOR UPDATE" if lock else ""),
        (org_id, SLACK_MEMORY_CATEGORY, SLACK_MEMORY_TITLE),
    )
    row = cursor.fetchone()
    return (str(row[0]), row[1] or "") if row else None


def _write(cursor, org_id: str, user_id: str, artifact_id: str, content: str) -> None:
    from services.artifacts.store import create_version

    cursor.execute(
        "UPDATE artifacts SET content = %s, last_edited_by = 'agent', updated_at = CURRENT_TIMESTAMP "
        "WHERE id = %s",
        (content, artifact_id),
    )
    create_version(cursor, artifact_id, org_id, user_id, content, source="agent")


def sync(cursor, org_id: str) -> None:
    """Make every channel's stored level what the memory says. The caller commits.

    The entry is locked while it is read: a level someone is setting right now
    is waited for, not overwritten with what the entry said a moment before.
    """
    entry = _entry(cursor, org_id, lock=True)
    named = parse(entry[1]) if entry else {}
    cursor.execute(
        "SELECT DISTINCT channel_id, lower(channel_name) FROM slack_channels "
        "WHERE org_id = %s AND provider = 'slack' AND is_member AND channel_name IS NOT NULL",
        (org_id,),
    )
    chosen = {channel_id: named[name] for channel_id, name in cursor.fetchall() if name in named}
    cursor.execute(
        "UPDATE detection_chat_cursors SET level = NULL "
        "WHERE org_id = %s AND level IS NOT NULL AND NOT (channel_id = ANY(%s))",
        (org_id, list(chosen)),
    )
    for channel_id, level in chosen.items():
        cursor.execute(
            """INSERT INTO detection_chat_cursors (org_id, channel_id, level) VALUES (%s, %s, %s)
               ON CONFLICT (org_id, channel_id) DO UPDATE SET level = EXCLUDED.level
               WHERE detection_chat_cursors.level IS DISTINCT FROM EXCLUDED.level""",
            (org_id, channel_id, level),
        )


def ensure_section(user_id: str, org_id: str) -> None:
    """Have the Slack memory exist and carry the section, so Aurora knows how to record a
    channel's level when asked. Never raises: the slider works without it."""
    from services.detection.runner import _org_cursor
    from services.memory.slack_memory import seed_slack_memory

    try:
        seed_slack_memory(user_id, org_id=org_id)
        with _org_cursor(user_id, org_id) as (cur, conn):
            if cur is None:
                return
            entry = _entry(cur, org_id, lock=True)
            if entry and _section_span(entry[1]) is None:
                _write(cur, org_id, user_id, entry[0], with_section(entry[1]))
            conn.commit()
    except Exception:
        logger.exception("[DetectionChat] Could not add the dynamic alerting section to the Slack memory")


def set_channel(cursor, org_id: str, user_id: str, channel_id: str, level: str) -> bool:
    """Write one channel's line and apply it. False when Aurora is not in the channel or
    the org has no Slack memory. The caller commits."""
    cursor.execute(
        "SELECT MAX(channel_name) FROM slack_channels "
        "WHERE org_id = %s AND provider = 'slack' AND channel_id = %s AND is_member",
        (org_id, channel_id),
    )
    row = cursor.fetchone()
    entry = _entry(cursor, org_id, lock=True)
    if not row or not row[0] or entry is None or level not in LEVELS:
        return False
    _write(cursor, org_id, user_id, entry[0], with_level(entry[1], row[0], level))
    sync(cursor, org_id)
    return True
