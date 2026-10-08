"""Slack's policy memory: the default "Slack" entry (category ``context``, title
``Slack``) seeded on connect. It's an ordinary memory entry — seeding only
creates the starting policy, never overwrites an existing one. The Slack wording
lives here; the seeding mechanics are platform-generic (``platform_memory``).
"""

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


def seed_slack_memory(user_id: str, org_id: str | None = None) -> bool:
    """Create the default "Slack" memory for an org if absent (idempotent,
    non-destructive). Returns True if a new entry was created. See
    ``seed_platform_memory`` for the ``org_id`` contract."""
    # Imported here, not at module top: platform_memory builds its spec registry
    # from this module's strings.
    from services.memory import platform_memory
    return platform_memory.seed_platform_memory(user_id, "slack", org_id=org_id)
