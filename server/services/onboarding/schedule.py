"""Which setup email, if any, is due for one account right now.

Pure: everything it needs is passed in, so the rules can be read and tested
on their own. At most one email per call; the sweep comes back later.

The run, from the guide's point of view:

* welcome, a few minutes after signup
* "X is connected", each time a new part of their stack connects
* a check-in about a day in, unless the whole stack is connected
* two short nudges (day 3, day 7) only while nothing has ever connected
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

WELCOME_DELAY = timedelta(minutes=3)
# Connects usually come in bursts; wait this long after the first one so the
# rest of the burst lands in the same email.
CONNECTED_GAP = timedelta(minutes=20)
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
    # Connected according to the latest check.
    parts_done: list[str] = field(default_factory=list)
    # Ever seen connected. A check that fails for a moment reports a tool as
    # disconnected, so "nothing is connected" decisions go by this instead.
    parts_ever: list[str] = field(default_factory=list)
    # Connected after enrollment and not announced yet, and since when.
    pending_parts: list[str] = field(default_factory=list)
    pending_since: datetime | None = None
    unsubscribed: bool = False


@dataclass(frozen=True)
class Due:
    kind: str
    parts: tuple[str, ...] = ()


def _connected_due(s: JourneyState, now: datetime) -> Due | None:
    # Only parts still connected right now: a tool removed during the wait
    # isn't announced (it stays pending in case it comes back).
    ready = tuple(p for p in s.pending_parts if p in s.parts_done and f"connected:{p}" not in s.sent)
    if not ready or s.pending_since is None or now - s.pending_since < CONNECTED_GAP:
        return None
    return Due("connected", ready)


def _followup_due(s: JourneyState, age: timedelta) -> Due | None:
    ever = set(s.parts_ever) | set(s.parts_done)
    if "checkin" not in s.sent and CHECKIN_AT <= age < CHECKIN_UNTIL and len(ever) < TOTAL_PARTS:
        return Due("checkin")
    if ever:
        return None
    if "nudge_day3" not in s.sent and NUDGE3_AT <= age < NUDGE3_UNTIL:
        return Due("nudge_day3")
    if "nudge_day7" not in s.sent and NUDGE7_AT <= age < NUDGE7_UNTIL:
        return Due("nudge_day7")
    return None


def due_email(s: JourneyState, now: datetime) -> Due | None:
    age = now - s.enrolled_at
    if s.unsubscribed or not s.email_verified or age > JOURNEY_LENGTH:
        return None
    if "welcome" not in s.sent:
        return Due("welcome") if age >= WELCOME_DELAY else None

    waiting = any(p in s.parts_done and f"connected:{p}" not in s.sent for p in s.pending_parts)
    if waiting:
        # Hold everything else until the burst has been announced.
        return _connected_due(s, now)

    last = max(s.sent.values())
    if now - last < QUIET_AFTER_ANY:
        return None
    return _followup_due(s, age)
