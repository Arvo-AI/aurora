"""What Aurora keeps about the chat it reads, and what a report turns into.

A report is a person saying something is wrong. It is never acted on as it
stands: it is handed to a read-only investigation, and only if that finds the
problem does an incident open (services/detection/chat_intake.py). Every report
ends in a state a person can read on the Checks page:

    new        kept, waiting for an investigation
    verifying  an investigation is looking
    confirmed  the investigation found it; an incident is open
    dismissed  the investigation found nothing, or did not finish
    merged     the same problem as a report already being looked at
    skipped    the org's daily limit on investigations was reached

What is stored is the one-line claim the model read, never the message or who
wrote it, and it is deleted after RETENTION_DAYS.

A report counts for nothing in a check's record. A check is credited by
incidents; a report reaches the record only by becoming one.
"""

from typing import Any, Dict, List, Optional, Tuple

from services.detection import STATE_RETIRED
from services.detection.chat_reader import MAX_KNOWN_ASSETS, Report

SOURCE_SLACK = "slack"
STATUS_NEW = "new"
STATUS_VERIFYING = "verifying"
STATUS_CONFIRMED = "confirmed"
STATUS_DISMISSED = "dismissed"
STATUS_MERGED = "merged"
STATUS_SKIPPED = "skipped"

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


# ---- channels -------------------------------------------------------------

_READABLE_CHANNELS = """
    SELECT DISTINCT channel_id FROM slack_channels
    WHERE org_id = %s AND provider = 'slack' AND is_member AND NOT is_archived
      AND NOT COALESCE(is_private, FALSE)
      AND (team_id IS NULL OR team_id = %s)
"""


def claim_channels(cursor, org_id: str, team_id: str, limit: int) -> List[Tuple[str, Optional[str]]]:
    """Take up to *limit* channels to read now: [(channel_id, last_ts or None if never read)].

    Only public channels of the workspace the token belongs to, that Aurora is
    in, and that nobody turned off. A private channel is never read: being
    invited to one to post notifications is not consent to read it.

    A channel where a message just arrived (see note_activity) goes first, then
    the one read longest ago. The claim keeps two overlapping turns off the
    same channel and expires by itself.
    """
    cursor.execute(
        "INSERT INTO detection_chat_cursors (org_id, channel_id) "
        "SELECT %s, channel_id FROM (" + _READABLE_CHANNELS + ") readable "
        "ON CONFLICT (org_id, channel_id) DO NOTHING",
        (org_id, org_id, team_id),
    )
    cursor.execute(
        """UPDATE detection_chat_cursors SET claimed_at = NOW()
           WHERE org_id = %s AND channel_id IN (
               SELECT c.channel_id FROM detection_chat_cursors c
               WHERE c.org_id = %s AND c.enabled
                 AND c.channel_id IN (""" + _READABLE_CHANNELS + """)
                 AND (c.claimed_at IS NULL OR c.claimed_at <= NOW() - %s * INTERVAL '1 second')
               ORDER BY c.pending_since NULLS LAST, c.updated_at NULLS FIRST, c.channel_id
               LIMIT %s
               FOR UPDATE SKIP LOCKED
           )
           RETURNING channel_id, last_ts""",
        (org_id, org_id, org_id, team_id, CHANNEL_CLAIM_SECONDS, limit),
    )
    return [(row[0], row[1]) for row in cursor.fetchall()]


def advance(cursor, org_id: str, channel_id: str, last_ts: str) -> None:
    """The channel was read up to *last_ts*."""
    cursor.execute(
        """UPDATE detection_chat_cursors
           SET last_ts = %s, updated_at = NOW(), claimed_at = NULL, pending_since = NULL,
               failed_attempts = 0
           WHERE org_id = %s AND channel_id = %s""",
        (last_ts, org_id, channel_id),
    )


def read_failed(cursor, org_id: str, channel_id: str, newest_ts: str) -> bool:
    """The channel's new messages could not be read. True when they are now given up on.

    They are tried again on the channel's next turn, MAX_FAILED_READS times.
    After that the cursor moves past them: a batch the model cannot take (too
    long, refused by the provider) would otherwise be sent, and paid for, on
    every turn for good.
    """
    cursor.execute(
        """UPDATE detection_chat_cursors
           SET failed_attempts = failed_attempts + 1, updated_at = NOW(), claimed_at = NULL
           WHERE org_id = %s AND channel_id = %s RETURNING failed_attempts""",
        (org_id, channel_id),
    )
    row = cursor.fetchone()
    if row is None or row[0] < MAX_FAILED_READS:
        return False
    advance(cursor, org_id, channel_id, newest_ts)
    return True


def note_activity(cursor, org_id: str, channel_id: str) -> None:
    """A message just arrived in *channel_id* (a Slack event): read it on the next turn."""
    cursor.execute(
        """UPDATE detection_chat_cursors SET pending_since = COALESCE(pending_since, NOW())
           WHERE org_id = %s AND channel_id = %s AND enabled""",
        (org_id, channel_id),
    )


def set_channel_enabled(cursor, org_id: str, channel_id: str, enabled: bool) -> bool:
    cursor.execute(
        "UPDATE detection_chat_cursors SET enabled = %s WHERE org_id = %s AND channel_id = %s",
        (enabled, org_id, channel_id),
    )
    return cursor.rowcount > 0


def list_channels(cursor, org_id: str) -> List[Dict[str, Any]]:
    """The channels Aurora reads or could read, for the page."""
    cursor.execute(
        """SELECT c.channel_id, MAX(s.channel_name), c.enabled, c.updated_at, c.last_ts IS NOT NULL
           FROM detection_chat_cursors c
           LEFT JOIN slack_channels s ON s.org_id = c.org_id AND s.channel_id = c.channel_id
           WHERE c.org_id = %s
           GROUP BY c.channel_id, c.enabled, c.updated_at, c.last_ts
           ORDER BY MAX(s.channel_name) NULLS LAST, c.channel_id""",
        (org_id,),
    )
    return [
        {"channel_id": r[0], "name": r[1] or r[0], "enabled": r[2],
         "last_read_at": r[3].isoformat() if r[3] and r[4] else None}
        for r in cursor.fetchall()
    ]


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
    if report.asset:
        cursor.execute(
            """SELECT id FROM detection_chat_signals
               WHERE org_id = %s AND lower(asset) = lower(%s) AND status <> %s
                 AND said_at >= to_timestamp(%s) - %s * INTERVAL '1 minute'
               ORDER BY said_at LIMIT 1""",
            (org_id, report.asset, STATUS_MERGED, float(report.ts), SAME_ISSUE_MINUTES),
        )
        row = cursor.fetchone()
        earlier = str(row[0]) if row else None
    cursor.execute(
        """INSERT INTO detection_chat_signals
           (org_id, source, channel_id, message_ts, message_count, said_at, asset, claim,
            status, merged_into)
           VALUES (%s, %s, %s, %s, %s, to_timestamp(%s), %s, %s, %s, %s)
           ON CONFLICT (org_id, channel_id, message_ts) DO NOTHING
           RETURNING id""",
        (org_id, SOURCE_SLACK, channel_id, report.ts, len(report.message_ts), float(report.ts),
         report.asset, report.claim, STATUS_MERGED if earlier else STATUS_NEW, earlier),
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
           RETURNING id, channel_id, asset, claim, said_at""",
        (STATUS_VERIFYING, org_id, STATUS_NEW, min(limit, left)),
    )
    return [
        {"id": str(r[0]), "channel_id": r[1], "asset": r[2], "claim": r[3], "said_at": r[4]}
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
        """SELECT id, asset, claim, channel_id FROM detection_chat_signals
           WHERE id = %s AND org_id = %s AND status = %s AND session_id = %s FOR UPDATE""",
        (report_id, org_id, STATUS_VERIFYING, session_id),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return {"id": str(row[0]), "asset": row[1], "claim": row[2], "channel_id": row[3]}


def conclude(cursor, report_id: str, status: str, detail: str,
             incident_id: Optional[str] = None) -> None:
    cursor.execute(
        """UPDATE detection_chat_signals
           SET status = %s, status_detail = %s, incident_id = %s, updated_at = NOW()
           WHERE id = %s""",
        (status, (detail or "")[:1000], incident_id, report_id),
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
    """Recent reports with how each ended, newest first, for the page."""
    cursor.execute(
        """SELECT r.id, r.channel_id, MAX(s.channel_name), r.said_at, r.asset, r.claim, r.status,
                  r.status_detail, r.incident_id, r.message_count
           FROM detection_chat_signals r
           LEFT JOIN slack_channels s ON s.org_id = r.org_id AND s.channel_id = r.channel_id
           WHERE r.org_id = %s
           GROUP BY r.id
           ORDER BY r.said_at DESC LIMIT %s""",
        (org_id, limit),
    )
    return [
        {"id": str(r[0]), "channel_id": r[1], "channel": r[2] or r[1], "said_at": r[3].isoformat(),
         "asset": r[4], "claim": r[5], "status": r[6], "status_detail": r[7],
         "incident_id": str(r[8]) if r[8] else None, "messages": r[9]}
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
