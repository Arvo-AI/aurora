"""How much Aurora does in each chat channel, and what it keeps about what it read.

Every channel has a level:

    off     Aurora does not read the channel.
    listen  Aurora reads it and looks into problems, and never posts there.
    sure    Aurora also replies in the thread, only once it has confirmed a problem.
    free    Aurora also answers questions and says what it checked when it found nothing.

The level comes from two places and nowhere else. The slider in Settings is the
org's level: off until an admin moves it, and it applies to every public
channel. A channel is set apart by telling Aurora, which keeps a line for it in
its Slack memory (services/detection/chat_memory.py). A private channel is off
until such a line names it. With the slider at off, nothing is read at all.

What Aurora notices is a report: a person saying something is wrong, or asking
something. It is never acted on as it stands. It is handed to a read-only look
(services/detection/chat_intake.py), and ends in a state a person can read on
the Checks page:

    new        kept, waiting to be looked into
    verifying  being looked into
    confirmed  a problem, found to be real; an incident is open
    answered   a question, answered in its thread
    dismissed  nothing found, nothing to add, or the look did not finish
    merged     the same problem as a report already being looked at
    skipped    the org's daily limit was reached

What is stored is the one-line claim the model read and what Aurora replied,
never the messages or who wrote them, and it is deleted after RETENTION_DAYS.

A report counts for nothing in a check's record. A check is credited by
incidents; a report reaches the record only by becoming one.
"""

import time
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from services.detection import STATE_RETIRED
from services.detection.chat_reader import KIND_PROBLEM, MAX_KNOWN_ASSETS, Report

SOURCE_SLACK = "slack"
STATUS_NEW = "new"
STATUS_VERIFYING = "verifying"
STATUS_CONFIRMED = "confirmed"
STATUS_ANSWERED = "answered"
STATUS_DISMISSED = "dismissed"
STATUS_MERGED = "merged"
STATUS_SKIPPED = "skipped"

LEVEL_OFF = "off"
LEVEL_LISTEN = "listen"
LEVEL_SURE = "sure"
LEVEL_FREE = "free"
# Quietest first. Each level does what the one before it does, and more.
LEVELS = (LEVEL_OFF, LEVEL_LISTEN, LEVEL_SURE, LEVEL_FREE)
# The slider: the org's level, kept as an org preference. Off until an admin moves it.
COMPANY_LEVEL_KEY = "slack_proactive_level"
# Aurora's unasked replies in one channel in a day. Past this it stays quiet there.
MAX_REPLIES_PER_CHANNEL_PER_DAY = 8
# A reply to something said longer ago than this would land in a dead thread.
REPLY_WITHIN_MINUTES = 90
# What other channels were talking about, offered to a look as possibly the same thing.
RELATED_WITHIN_HOURS = 48
MAX_RELATED = 20

KNOWN_ASSET_INCIDENT_DAYS = 90
# Reports about the same asset this close together are one problem.
SAME_ISSUE_MINUTES = 60
# Each one is a model-driven investigation. Past this, reports are kept and marked skipped.
MAX_VERIFICATIONS_PER_ORG_PER_DAY = 20
# Each one is a model call over up to a channel's new messages.
MAX_READS_PER_ORG_PER_DAY = 600
# A channel whose messages could not be read this many times running is moved
# past them, so one unreadable batch is not sent to the model forever.
MAX_FAILED_READS = 3
# A report makes the asset's checks run now, but not more often than this.
RUN_NOW_COOLDOWN_SECONDS = 300
# An investigation that has not answered after this long is not going to.
VERIFICATION_TIMEOUT_HOURS = 2
RETENTION_DAYS = 30
CHANNEL_CLAIM_SECONDS = 300
# Threads a channel remembers as having a new reply, until they are read.
MAX_NOTED_THREADS = 30


def _hold(cursor, what: str) -> None:
    """Let one transaction at a time past this point for *what*, until it commits."""
    cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (what,))


def known_assets(cursor, org_id: str) -> List[str]:
    """Names the model is offered: assets Aurora watches or has had incidents on."""
    cursor.execute(
        """SELECT asset FROM (
               SELECT asset, 0 AS source FROM detection_checks
               WHERE org_id = %s AND state <> %s
               UNION
               SELECT alert_service, 1 FROM incidents
               WHERE org_id = %s AND alert_service IS NOT NULL
                 AND lower(alert_service) NOT IN ('', 'unknown')
                 AND created_at >= NOW() - %s * INTERVAL '1 day'
           ) assets
           GROUP BY asset ORDER BY MIN(source), asset LIMIT %s""",
        (org_id, STATE_RETIRED, org_id, KNOWN_ASSET_INCIDENT_DAYS, MAX_KNOWN_ASSETS),
    )
    seen, assets = set(), []
    for (asset,) in cursor.fetchall():
        key = (asset or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            assets.append(asset.strip())
    return assets


# ---- levels ---------------------------------------------------------------

def company_level(org_id: str) -> str:
    """Where the org's slider is: the level of every public channel that has no line of its
    own in the Slack memory. Off when unset or unreadable."""
    from utils.auth.stateless_auth import get_org_preference

    value = get_org_preference(org_id, COMPANY_LEVEL_KEY, default=LEVEL_OFF)
    return value if value in LEVELS else LEVEL_OFF


def set_company_level(org_id: str, level: str) -> bool:
    """Store where the slider is. False when it did not take."""
    from utils.auth.stateless_auth import store_org_preference

    from utils.auth.stateless_auth import get_org_preference

    store_org_preference(org_id, COMPANY_LEVEL_KEY, level)
    # Strict: a read that fails must not pass for "off was stored".
    return get_org_preference(org_id, COMPANY_LEVEL_KEY, default=None, strict=True) == level


def speaks(level: str, kind: str, real: bool) -> bool:
    """Whether Aurora may reply in a channel at *level* about this outcome."""
    if level == LEVEL_FREE:
        return True
    return level == LEVEL_SURE and kind == KIND_PROBLEM and real


# The channels of this workspace Aurora is in, one row each, with the level
# that applies. Parameters: slider, slider, org_id, team_id, org_id.
_CHANNELS = """
    SELECT s.channel_id, s.name, s.is_private, c.level AS chosen,
           CASE WHEN %s = 'off' THEN 'off'
                ELSE COALESCE(c.level, CASE WHEN s.is_private THEN 'off' ELSE %s END) END AS level,
           c.last_ts, c.updated_at
    FROM (
        SELECT channel_id, MAX(channel_name) AS name, BOOL_OR(COALESCE(is_private, FALSE)) AS is_private
        FROM slack_channels
        WHERE org_id = %s AND provider = 'slack' AND is_member AND NOT is_archived
          AND (team_id IS NULL OR team_id = %s)
        GROUP BY channel_id
    ) s
    LEFT JOIN detection_chat_cursors c ON c.org_id = %s AND c.channel_id = s.channel_id
"""


def _channel_params(org_id: str, team_id: Optional[str], company: str) -> tuple:
    return (company, company, org_id, team_id, org_id)


def channel_level(cursor, org_id: str, team_id: Optional[str], channel_id: str, company: str) -> Optional[Dict[str, Any]]:
    """One channel as {level, name, is_private}, or None if Aurora is not in it."""
    cursor.execute(
        "SELECT level, name, is_private FROM (" + _CHANNELS + ") ch "
        "WHERE channel_id = %s",
        _channel_params(org_id, team_id, company) + (channel_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return {"level": row[0], "name": row[1] or channel_id, "is_private": row[2]}


def list_channels(cursor, org_id: str, team_id: Optional[str], company: str) -> List[Dict[str, Any]]:
    """Every channel Aurora is in and the level that applies, for the page."""
    cursor.execute(
        "SELECT channel_id, name, is_private, chosen, level, last_ts IS NOT NULL, updated_at "
        "FROM (" + _CHANNELS + ") ch ORDER BY name NULLS LAST, channel_id",
        _channel_params(org_id, team_id, company),
    )
    return [
        {"channel_id": r[0], "name": r[1] or r[0], "is_private": r[2], "chosen": r[3], "level": r[4],
         "last_read_at": r[6].isoformat() if r[5] and r[6] else None}
        for r in cursor.fetchall()
    ]


# ---- reading turns --------------------------------------------------------

class Turn(NamedTuple):
    """One channel taken to read now. *claim* is what advance and read_failed must be
    given back; *threads* are the threads with a reply the history page may not show."""
    channel_id: str
    last_ts: Optional[str]
    level: str
    claim: Any
    threads: Dict[str, str]


def reads_to_claim(cursor, org_id: str, limit: int) -> int:
    """How many channels this turn may take: what is left of the day's reads, less the
    channels other turns hold and have not read yet. Holds the org's turns apart until
    the caller commits, so two of them cannot both spend the same allowance."""
    _hold(cursor, "detection_chat_reads:%s" % org_id)
    cursor.execute(
        "SELECT COUNT(*) FROM detection_chat_cursors "
        "WHERE org_id = %s AND claimed_at > NOW() - %s * INTERVAL '1 second'",
        (org_id, CHANNEL_CLAIM_SECONDS),
    )
    held = cursor.fetchone()[0]
    return max(0, min(limit, reads_left_today(cursor, org_id) - held))


def claim_channels(cursor, org_id: str, team_id: str, company: str,
                   limit: int, quiet_after: int = 0) -> List[Turn]:
    """Take up to *limit* channels to read now, as Turns.

    Only channels of the workspace the token belongs to, that Aurora is in, and
    whose level is not off. A private channel is off until someone sets it:
    being invited to one to post notifications is not consent to read it.

    A channel where a message just arrived (see note_activity) goes first, then
    the one read longest ago. A channel where none arrived is left alone until
    it was last read *quiet_after* seconds ago, but only a channel Aurora has
    been told of a message in before: where Slack sends no message events (a
    private channel, an app without the subscription) silence says nothing,
    and the channel keeps its turn. The claim keeps two overlapping turns off
    the same channel and expires by itself.
    """
    params = _channel_params(org_id, team_id, company)
    cursor.execute(
        "INSERT INTO detection_chat_cursors (org_id, channel_id) "
        "SELECT %s, channel_id FROM (" + _CHANNELS + ") ch WHERE level <> 'off' "
        "ON CONFLICT (org_id, channel_id) DO NOTHING",
        (org_id,) + params,
    )
    cursor.execute(
        """WITH readable AS (SELECT channel_id, level FROM (""" + _CHANNELS + """) ch WHERE level <> 'off'),
           taken AS (
               SELECT c.channel_id FROM detection_chat_cursors c
               JOIN readable r ON r.channel_id = c.channel_id
               WHERE c.org_id = %s
                 AND (c.claimed_at IS NULL OR c.claimed_at <= NOW() - %s * INTERVAL '1 second')
                 AND (c.pending_since IS NOT NULL OR c.updated_at IS NULL OR c.noted_ts IS NULL
                      OR c.updated_at <= NOW() - %s * INTERVAL '1 second')
               ORDER BY c.pending_since NULLS LAST, c.updated_at NULLS FIRST, c.channel_id
               LIMIT %s
               FOR UPDATE OF c SKIP LOCKED
           )
           UPDATE detection_chat_cursors c SET claimed_at = NOW()
           FROM readable r
           WHERE c.org_id = %s AND c.channel_id = r.channel_id
             AND c.channel_id IN (SELECT channel_id FROM taken)
           RETURNING c.channel_id, c.last_ts, r.level, c.claimed_at, c.threads,
                     c.pending_since IS NULL, c.pending_since, c.updated_at IS NOT NULL, c.updated_at""",
        params + (org_id, CHANNEL_CLAIM_SECONDS, quiet_after, limit, org_id),
    )
    return [Turn(row[0], row[1], row[2], row[3], dict(row[4] or {})) for row in _as_chosen(cursor.fetchall())]


def _as_chosen(rows: List[tuple]) -> List[tuple]:
    """The claimed rows in the order they were chosen in: an UPDATE returns them
    in any order. Same order as claim_channels asks for: pending first and the
    longest pending first, then never read, then read longest ago."""
    return sorted(rows, key=lambda row: (row[5], row[6] or 0, row[7], row[8] or 0, row[0]))


def advance(cursor, org_id: str, channel_id: str, last_ts: str, claim: Any,
            read_upto: Optional[str] = None) -> None:
    """The channel was read up to *last_ts*, by the turn holding *claim*.

    A turn that outlived its claim changes nothing: another has the channel by
    then, and its cursor is the newer one.

    *read_upto* is the moment the reading took the channel at; *last_ts*, the
    newest message it found, can be earlier. A thread noted as active is
    dropped once its reply is no later than that moment: the thread was read
    that far, whether or not the reply was still there. Otherwise after a day.

    The channel still has something to read when a message noted in it is
    later than the reading (noted_ts, the message's own Slack time, against
    *read_upto*: a reading taken up again over several turns has a moment that
    is minutes old by the time it ends), or a noted reply is. It then stays in
    line for the next turn.
    """
    unread = """SELECT noted.key, noted.value FROM jsonb_each_text(threads) AS noted
                WHERE noted.value::float8 > %s::float8
                  AND noted.value::float8 > EXTRACT(EPOCH FROM NOW()) - 86400"""
    upto = read_upto or last_ts
    cursor.execute(
        """UPDATE detection_chat_cursors
           SET last_ts = %s, updated_at = NOW(), claimed_at = NULL, failed_attempts = 0,
               pending_since = CASE WHEN noted_ts::float8 > %s::float8 OR EXISTS (""" + unread + """)
                                    THEN NOW() END,
               threads = COALESCE((SELECT jsonb_object_agg(u.key, u.value) FROM (""" + unread + """) u),
                                  '{}'::jsonb)
           WHERE org_id = %s AND channel_id = %s AND claimed_at = %s""",
        (last_ts, upto, upto, upto, org_id, channel_id, claim),
    )


def put_back(cursor, org_id: str, channel_id: str, claim: Any, first: bool = False) -> None:
    """The turn holding *claim* did not read the channel, through no fault of the
    channel: it is free to be taken again, and is where it was in the order.

    *first* puts it ahead of every other channel. That is for a reading stopped
    part way, which a later turn goes on with (see chat_pace).
    """
    cursor.execute(
        """UPDATE detection_chat_cursors
           SET claimed_at = NULL,
               pending_since = CASE WHEN %s THEN 'epoch'::timestamptz ELSE pending_since END
           WHERE org_id = %s AND channel_id = %s AND claimed_at = %s""",
        (first, org_id, channel_id, claim),
    )


def read_failed(cursor, org_id: str, channel_id: str, newest_ts: str, claim: Any,
                read_upto: Optional[str] = None) -> bool:
    """The channel's new messages could not be read. True when they are now given up on.

    They are tried again on the channel's next turn, MAX_FAILED_READS times.
    After that the cursor moves past them: a batch the model cannot take (too
    long, refused by the provider) would otherwise be sent, and paid for, on
    every turn for good.
    """
    cursor.execute(
        """UPDATE detection_chat_cursors
           SET failed_attempts = failed_attempts + 1, updated_at = NOW()
           WHERE org_id = %s AND channel_id = %s AND claimed_at = %s RETURNING failed_attempts""",
        (org_id, channel_id, claim),
    )
    row = cursor.fetchone()
    if row is None:
        return False
    if row[0] < MAX_FAILED_READS:
        cursor.execute(
            "UPDATE detection_chat_cursors SET claimed_at = NULL WHERE org_id = %s AND channel_id = %s",
            (org_id, channel_id),
        )
        return False
    advance(cursor, org_id, channel_id, newest_ts, claim, read_upto)
    return True


def note_activity(cursor, org_id: str, channel_id: str, thread_ts: Optional[str] = None,
                  ts: Optional[str] = None) -> None:
    """A message just arrived in *channel_id* (a Slack event): read it on the next turn.

    The channel keeps its place in line from the first message not yet read.
    The message's own time is kept as well (noted_ts, the newest noted): a
    reading that took the channel before the message was there does not clear
    it (see advance).

    A reply in a thread is noted with its thread: the thread's first message can
    be further back than the history the next reading takes, and the reply
    would not be found from there. At most MAX_NOTED_THREADS a channel.
    """
    # An event without a usable time is taken as of now.
    noted = ts if ts and _is_ts(ts) else "%.6f" % time.time()
    cursor.execute(
        """UPDATE detection_chat_cursors
           SET pending_since = COALESCE(pending_since, NOW()),
               noted_ts = CASE WHEN noted_ts IS NULL OR noted_ts::float8 < %s::float8 THEN %s ELSE noted_ts END
           WHERE org_id = %s AND channel_id = %s""",
        (noted, noted, org_id, channel_id),
    )
    if not thread_ts or not ts or thread_ts == ts or not (_is_ts(thread_ts) and _is_ts(ts)):
        return
    cursor.execute(
        """UPDATE detection_chat_cursors SET threads = jsonb_set(threads, ARRAY[%s], to_jsonb(%s::text))
           WHERE org_id = %s AND channel_id = %s
             AND (threads ? %s OR (SELECT COUNT(*) FROM jsonb_object_keys(threads)) < %s)""",
        (thread_ts, ts, org_id, channel_id, thread_ts, MAX_NOTED_THREADS),
    )


def _is_ts(value: str) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


# ---- reads ----------------------------------------------------------------

def reads_left_today(cursor, org_id: str) -> int:
    cursor.execute(
        "SELECT COUNT(*) FROM detection_chat_reads "
        "WHERE org_id = %s AND read_at >= NOW() - INTERVAL '1 day'",
        (org_id,),
    )
    return max(0, MAX_READS_PER_ORG_PER_DAY - cursor.fetchone()[0])


def record_read(cursor, org_id: str, channel_id: str, messages: int, reports: int, ok: bool) -> None:
    """One model call over a channel: the audit of what was read, and what the limit counts."""
    cursor.execute(
        "INSERT INTO detection_chat_reads (org_id, channel_id, messages, reports, ok) "
        "VALUES (%s, %s, %s, %s, %s)",
        (org_id, channel_id, messages, reports, ok),
    )


# ---- reports --------------------------------------------------------------

def keep_report(cursor, org_id: str, channel_id: str, report: Report) -> Optional[str]:
    """Store a report. Returns its id when it is a new problem to look into, else None.

    None for a message already kept, and for a report about an asset that
    another report from the last hour already covers: that one is being, or
    has been, looked into, and five people saying "checkout is down" are one
    investigation. The repeat is still kept, marked merged.
    """
    earlier = None
    if report.asset and report.kind == KIND_PROBLEM:
        # Two channels read at once must not each find no earlier report and start a look each.
        _hold(cursor, "detection_chat_asset:%s:%s" % (org_id, report.asset.lower()))
        cursor.execute(
            """SELECT id FROM detection_chat_signals
               WHERE org_id = %s AND lower(asset) = lower(%s) AND status <> %s AND kind = %s
                 AND said_at BETWEEN to_timestamp(%s) - %s * INTERVAL '1 minute'
                                 AND to_timestamp(%s) + %s * INTERVAL '1 minute'
               ORDER BY said_at LIMIT 1""",
            (org_id, report.asset, STATUS_MERGED, KIND_PROBLEM, float(report.ts), SAME_ISSUE_MINUTES,
             float(report.ts), SAME_ISSUE_MINUTES),
        )
        row = cursor.fetchone()
        earlier = str(row[0]) if row else None
    cursor.execute(
        """INSERT INTO detection_chat_signals
           (org_id, source, channel_id, message_ts, thread_ts, message_count, said_at, kind,
            asset, claim, status, merged_into)
           VALUES (%s, %s, %s, %s, %s, %s, to_timestamp(%s), %s, %s, %s, %s, %s)
           ON CONFLICT (org_id, channel_id, message_ts) DO NOTHING
           RETURNING id""",
        (org_id, SOURCE_SLACK, channel_id, report.ts, report.thread_ts or report.ts,
         len(report.message_ts), float(report.ts), report.kind, report.asset, report.claim,
         STATUS_MERGED if earlier else STATUS_NEW, earlier),
    )
    row = cursor.fetchone()
    return str(row[0]) if row and not earlier else None


def run_checks_now(cursor, org_id: str, asset: str) -> int:
    """Make the checks on *asset* due, so they have answered by the time someone looks.

    Not ones that ran in the last few minutes: otherwise anyone could make an
    asset's checks run every minute by posting about it.
    """
    cursor.execute(
        """UPDATE detection_checks SET last_run_at = NULL
           WHERE org_id = %s AND lower(asset) = lower(%s) AND state <> %s AND claimed_at IS NULL
             AND last_run_at <= NOW() - %s * INTERVAL '1 second'""",
        (org_id, asset, STATE_RETIRED, RUN_NOW_COOLDOWN_SECONDS),
    )
    return cursor.rowcount


def take_for_verification(cursor, org_id: str, limit: int) -> List[Dict[str, Any]]:
    """Move up to *limit* new reports to verifying and return them, oldest first.

    Reports beyond the org's daily limit are marked skipped instead, so they
    are neither investigated late nor left looking as if they still will be.
    """
    # Two turns of the same org must not both count the same allowance.
    _hold(cursor, "detection_chat_looks:" + org_id)
    cursor.execute(
        "SELECT COUNT(*) FROM detection_chat_signals "
        "WHERE org_id = %s AND verify_started_at >= NOW() - INTERVAL '1 day'",
        (org_id,),
    )
    left = MAX_VERIFICATIONS_PER_ORG_PER_DAY - cursor.fetchone()[0]
    if left <= 0:
        cursor.execute(
            "UPDATE detection_chat_signals SET status = %s, status_detail = %s, updated_at = NOW() "
            "WHERE org_id = %s AND status = %s",
            (STATUS_SKIPPED, "The daily limit of %d investigations from chat was reached."
             % MAX_VERIFICATIONS_PER_ORG_PER_DAY, org_id, STATUS_NEW),
        )
        return []
    cursor.execute(
        """UPDATE detection_chat_signals
           SET status = %s, verify_started_at = NOW(), updated_at = NOW()
           WHERE id IN (
               SELECT id FROM detection_chat_signals
               WHERE org_id = %s AND status = %s
               ORDER BY said_at LIMIT %s FOR UPDATE SKIP LOCKED
           )
           RETURNING id, channel_id, asset, claim, said_at, kind""",
        (STATUS_VERIFYING, org_id, STATUS_NEW, min(limit, left)),
    )
    return [
        {"id": str(r[0]), "channel_id": r[1], "asset": r[2], "claim": r[3], "said_at": r[4],
         "kind": r[5]}
        for r in cursor.fetchall()
    ]


def related_reports(cursor, org_id: str, team_id: Optional[str], report: Dict[str, Any]) -> List[Dict[str, Any]]:
    """What Aurora noticed lately in other public channels, newest first.

    Offered to a look so it can say "this is the same thing". Nothing from a
    private channel is ever offered: what is said there stays there.
    """
    cursor.execute(
        """SELECT r.id, ch.name, r.said_at, r.kind, r.claim, r.status
           FROM detection_chat_signals r
           JOIN (""" + _CHANNELS + """) ch ON ch.channel_id = r.channel_id
           WHERE r.org_id = %s AND r.id <> %s AND r.channel_id <> %s AND NOT ch.is_private
             AND r.status <> %s AND r.said_at >= NOW() - %s * INTERVAL '1 hour'
           ORDER BY r.said_at DESC LIMIT %s""",
        _channel_params(org_id, team_id, LEVEL_OFF)
        + (org_id, report["id"], report["channel_id"], STATUS_MERGED, RELATED_WITHIN_HOURS, MAX_RELATED),
    )
    return [
        {"id": str(r[0]), "channel": r[1] or "", "said_at": r[2], "kind": r[3], "claim": r[4],
         "status": r[5]}
        for r in cursor.fetchall()
    ]


def set_session(cursor, report_id: str, session_id: str) -> None:
    cursor.execute(
        "UPDATE detection_chat_signals SET session_id = %s WHERE id = %s", (session_id, report_id),
    )


def back_to_new(cursor, report_id: str) -> None:
    """The investigation could not be started. It does not count against the daily limit."""
    cursor.execute(
        "UPDATE detection_chat_signals SET status = %s, verify_started_at = NULL, session_id = NULL "
        "WHERE id = %s AND status = %s",
        (STATUS_NEW, report_id, STATUS_VERIFYING),
    )


def report_under_verification(cursor, org_id: str, report_id: str,
                              session_id: str) -> Optional[Dict[str, Any]]:
    """The report, if it is this org's, still being verified, and by this very session."""
    cursor.execute(
        """SELECT id, asset, claim, channel_id, kind FROM detection_chat_signals
           WHERE id = %s AND org_id = %s AND status = %s AND session_id = %s FOR UPDATE""",
        (report_id, org_id, STATUS_VERIFYING, session_id),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return {"id": str(row[0]), "asset": row[1], "claim": row[2], "channel_id": row[3], "kind": row[4]}


def conclude(cursor, report_id: str, status: str, detail: str, reply: str = "",
             related_to: Optional[str] = None) -> None:
    """How the look ended, and what it wants said. Saying it is services/detection/chat_voice.py."""
    cursor.execute(
        """UPDATE detection_chat_signals
           SET status = %s, status_detail = %s, reply = NULLIF(%s, ''), related_to = %s,
               updated_at = NOW()
           WHERE id = %s""",
        (status, (detail or "")[:1000], reply, related_to, report_id),
    )


def other_public_report(cursor, org_id: str, team_id: Optional[str], report_id: str,
                        other_id: str) -> Optional[Dict[str, Any]]:
    """*other_id* if it is a recent report of this org in a different, public channel."""
    cursor.execute(
        """SELECT o.id, o.channel_id, o.thread_ts, o.claim
           FROM detection_chat_signals o
           JOIN detection_chat_signals mine ON mine.id = %s AND mine.org_id = o.org_id
           JOIN (""" + _CHANNELS + """) ch ON ch.channel_id = o.channel_id
           WHERE o.id = %s AND o.org_id = %s AND o.channel_id <> mine.channel_id
             AND NOT ch.is_private AND o.said_at >= NOW() - %s * INTERVAL '1 hour'""",
        (report_id,) + _channel_params(org_id, team_id, LEVEL_OFF)
        + (other_id, org_id, RELATED_WITHIN_HOURS),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return {"id": str(row[0]), "channel_id": row[1], "thread_ts": row[2], "claim": row[3]}


# ---- speaking -------------------------------------------------------------

def to_say(cursor, org_id: str, report_id: str) -> Optional[Dict[str, Any]]:
    """A concluded report and what is needed to reply to it, locked. None if there is none."""
    cursor.execute(
        """SELECT id, channel_id, thread_ts, message_ts, kind, status, reply, reply_ts, incident_id,
                  related_to, claim, said_at >= NOW() - %s * INTERVAL '1 minute'
           FROM detection_chat_signals WHERE id = %s AND org_id = %s FOR UPDATE""",
        (REPLY_WITHIN_MINUTES, report_id, org_id),
    )
    r = cursor.fetchone()
    if r is None:
        return None
    return {"id": str(r[0]), "channel_id": r[1], "thread_ts": r[2] or r[3], "kind": r[4],
            "status": r[5], "reply": r[6] or "", "reply_ts": r[7],
            "incident_id": str(r[8]) if r[8] else None,
            "related_to": str(r[9]) if r[9] else None, "claim": r[10], "fresh": r[11]}


def replies_left_today(cursor, org_id: str, channel_id: str) -> int:
    """How many more unasked posts the channel may get today. Replies and pointers both count.

    Holds the channel until the caller commits, so two posts cannot both take the last place.
    """
    _hold(cursor, "detection_chat_says:%s:%s" % (org_id, channel_id))
    cursor.execute(
        """SELECT COUNT(*) FILTER (WHERE replied_at >= NOW() - INTERVAL '1 day')
                + COUNT(*) FILTER (WHERE pointed_at >= NOW() - INTERVAL '1 day')
           FROM detection_chat_signals WHERE org_id = %s AND channel_id = %s""",
        (org_id, channel_id),
    )
    return max(0, MAX_REPLIES_PER_CHANNEL_PER_DAY - cursor.fetchone()[0])


def claim_reply(cursor, report_id: str) -> bool:
    """Take the one right to reply to a report. False when it was already taken."""
    cursor.execute(
        "UPDATE detection_chat_signals SET replied_at = NOW() WHERE id = %s AND replied_at IS NULL",
        (report_id,),
    )
    return cursor.rowcount > 0


def record_reply(cursor, report_id: str, reply_ts: str) -> None:
    cursor.execute(
        "UPDATE detection_chat_signals SET reply_ts = %s, replied_at = NOW() WHERE id = %s",
        (reply_ts, report_id),
    )


def release_reply(cursor, report_id: str) -> None:
    """The post did not go out: the reply was not made."""
    cursor.execute(
        "UPDATE detection_chat_signals SET replied_at = NULL WHERE id = %s AND reply_ts IS NULL",
        (report_id,),
    )


def count_reply_check(cursor, report_id: str) -> int:
    """One more reply for this report failed its evidence check. Returns how many have."""
    cursor.execute(
        "UPDATE detection_chat_signals SET reply_checks = reply_checks + 1 WHERE id = %s "
        "RETURNING reply_checks",
        (report_id,),
    )
    row = cursor.fetchone()
    return row[0] if row else 0


def claim_pointer(cursor, org_id: str, other_id: str, report_id: str, within_hours: int) -> bool:
    """Link *other_id* to *report_id*, once, if its thread is still recent."""
    cursor.execute(
        """UPDATE detection_chat_signals SET related_to = %s, pointed_at = NOW()
           WHERE id = %s AND org_id = %s AND related_to IS NULL
             AND said_at >= NOW() - %s * INTERVAL '1 hour'""",
        (report_id, other_id, org_id, within_hours),
    )
    return cursor.rowcount > 0


def release_pointer(cursor, org_id: str, other_id: str, report_id: str) -> None:
    """The pointer did not go out: *other_id* was not told, and may be told later."""
    cursor.execute(
        "UPDATE detection_chat_signals SET related_to = NULL, pointed_at = NULL "
        "WHERE id = %s AND org_id = %s AND related_to = %s",
        (other_id, org_id, report_id),
    )


def tidy(cursor, org_id: str) -> Tuple[int, int]:
    """Close investigations that never answered, and delete what is past retention.

    Returns (closed, deleted). A report whose incident is still on record goes
    with everything else: the incident holds what happened.
    """
    cursor.execute(
        """UPDATE detection_chat_signals
           SET status = %s, status_detail = %s, updated_at = NOW()
           WHERE org_id = %s AND status = %s
             AND verify_started_at <= NOW() - %s * INTERVAL '1 hour'""",
        (STATUS_DISMISSED, "The investigation did not finish.", org_id, STATUS_VERIFYING,
         VERIFICATION_TIMEOUT_HOURS),
    )
    closed = cursor.rowcount
    cursor.execute(
        "DELETE FROM detection_chat_signals WHERE org_id = %s "
        "AND created_at <= NOW() - %s * INTERVAL '1 day'",
        (org_id, RETENTION_DAYS),
    )
    deleted = cursor.rowcount
    cursor.execute(
        "DELETE FROM detection_chat_reads WHERE org_id = %s "
        "AND read_at <= NOW() - %s * INTERVAL '1 day'",
        (org_id, RETENTION_DAYS),
    )
    return closed, deleted


def list_reports(cursor, org_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    """Recent reports with how each ended, newest first, for the page.

    Not those from a private channel: the page is open to the whole org, and
    what was said in a private channel is for the people in it.
    """
    cursor.execute(
        """SELECT r.id, r.channel_id, MAX(s.channel_name), r.said_at, r.asset, r.claim, r.status,
                  r.status_detail, r.incident_id, r.message_count, r.kind,
                  CASE WHEN r.reply_ts IS NOT NULL THEN r.reply END
           FROM detection_chat_signals r
           JOIN slack_channels s ON s.org_id = r.org_id AND s.channel_id = r.channel_id
           WHERE r.org_id = %s
           GROUP BY r.id
           HAVING NOT BOOL_OR(COALESCE(s.is_private, FALSE))
           ORDER BY r.said_at DESC LIMIT %s""",
        (org_id, limit),
    )
    return [
        {"id": str(r[0]), "channel_id": r[1], "channel": r[2] or r[1], "said_at": r[3].isoformat(),
         "asset": r[4], "claim": r[5], "status": r[6], "status_detail": r[7],
         "incident_id": str(r[8]) if r[8] else None, "messages": r[9], "kind": r[10],
         "reply": r[11]}
        for r in cursor.fetchall()
    ]


def failing_checks_on(cursor, org_id: str, asset: str) -> List[Dict[str, Any]]:
    """Aurora's own checks on *asset* and what each last saw, for the investigation."""
    cursor.execute(
        """SELECT name, last_status, COALESCE(last_observed, ''), prediction
           FROM detection_checks
           WHERE org_id = %s AND lower(asset) = lower(%s) AND state <> %s
           ORDER BY (last_status = 'fail') DESC, name LIMIT 10""",
        (org_id, asset, STATE_RETIRED),
    )
    return [
        {"name": r[0], "last_status": r[1], "last_observed": r[2], "prediction": r[3]}
        for r in cursor.fetchall()
    ]
