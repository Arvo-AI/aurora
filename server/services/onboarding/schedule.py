"""Which setup email, if any, is due for one account right now.

Pure: everything it needs is passed in, so the rules can be read and tested
on their own. At most one email per call; the sweep comes back later.

The run, from the guide's point of view:

* welcome, a few minutes after signup
* "X is connected", each time a new part of their stack connects
* a check-in about a day in, unless the whole stack is connected
* two short nudges (day 3, day 7) only while nothing is connected
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

WELCOME_DELAY = timedelta(minutes=3)
CONNECTED_GAP = timedelta(minutes=20)  # bursts of connects become one email
CHECKIN_AT = timedelta(hours=22)
CHECKIN_UNTIL = timedelta(days=4)
NUDGE3_AT, NUDGE3_UNTIL = timedelta(days=3), timedelta(days=6)
NUDGE7_AT, NUDGE7_UNTIL = timedelta(days=7), timedelta(days=10)
QUIET_AFTER_ANY = timedelta(hours=12)  # check-ins and nudges never crowd another email
JOURNEY_LENGTH = timedelta(days=14)

TOTAL_PARTS = 4


@dataclass
class JourneyState:
    enrolled_at: datetime
    email_verified: bool
    # kind -> when it went out. Connected emails are recorded per part,
    # as "connected:<part>", so each part is announced once.
    sent: dict[str, datetime] = field(default_factory=dict)
    parts_done: list[str] = field(default_factory=list)
    # Parts that connected after enrollment and haven't been announced.
    pending_parts: list[str] = field(default_factory=list)
    unsubscribed: bool = False


@dataclass(frozen=True)
class Due:
    kind: str
    parts: tuple[str, ...] = ()


def _last_sent(s: JourneyState) -> datetime | None:
    return max(s.sent.values(), default=None)


def due_email(s: JourneyState, now: datetime) -> Due | None:
    age = now - s.enrolled_at
    if s.unsubscribed or not s.email_verified or age > JOURNEY_LENGTH:
        return None

    if "welcome" not in s.sent:
        return Due("welcome") if age >= WELCOME_DELAY else None

    last = _last_sent(s)
    since_last = now - last if last else age

    unannounced = tuple(p for p in s.pending_parts if f"connected:{p}" not in s.sent)
    if unannounced:
        return Due("connected", unannounced) if since_last >= CONNECTED_GAP else None

    if since_last < QUIET_AFTER_ANY:
        return None

    nothing = not s.parts_done
    if "checkin" not in s.sent and CHECKIN_AT <= age < CHECKIN_UNTIL and len(s.parts_done) < TOTAL_PARTS:
        return Due("checkin")
    if nothing and "nudge_day3" not in s.sent and NUDGE3_AT <= age < NUDGE3_UNTIL:
        return Due("nudge_day3")
    if nothing and "nudge_day7" not in s.sent and NUDGE7_AT <= age < NUDGE7_UNTIL:
        return Due("nudge_day7")
    return None
