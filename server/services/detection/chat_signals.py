"""What a problem report in chat does.

A person saying "checkout is broken" is evidence, and so is one of Aurora's
unproven checks failing on checkout. Neither is enough to interrupt anyone.
Together they are two independent sources saying the same thing about the same
asset, and that opens a case.

A report on its own does three quiet things:

- it is kept (the claim Aurora read, not the message), so a later incident can
  be traced back to who noticed first;
- it makes every check on that asset run now instead of at its next interval;
- it counts as evidence when a check's fire is graded: a person noticing the
  problem is something the world did, not something Aurora did.

Nothing here posts to chat. Aurora does not reply to the report.
"""

from typing import List, Optional, Tuple

from services.detection import STATE_RETIRED, STATE_SILENT
from services.detection.chat_reader import KIND_REPORT, MAX_KNOWN_ASSETS, Reading

SOURCE_SLACK = "slack"
# How long a report and a failing check may be apart and still be about the same thing.
CORROBORATION_MINUTES = 60
KNOWN_ASSET_INCIDENT_DAYS = 90


def known_assets(cursor, org_id: str) -> List[str]:
    """Assets a report may be attached to: ones Aurora watches or has had incidents on."""
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


def next_channel(cursor, org_id: str) -> Optional[Tuple[str, Optional[str]]]:
    """The channel read longest ago: (channel_id, last_ts or None when never read).

    One channel per org per tick, so reading stays far inside Slack's limits on
    channel history however many channels Aurora is in.
    """
    cursor.execute(
        """SELECT s.channel_id, c.last_ts
           FROM (SELECT DISTINCT channel_id FROM slack_channels
                 WHERE org_id = %s AND provider = 'slack' AND is_member AND NOT is_archived) s
           LEFT JOIN detection_chat_cursors c ON c.org_id = %s AND c.channel_id = s.channel_id
           ORDER BY c.updated_at NULLS FIRST, s.channel_id
           LIMIT 1""",
        (org_id, org_id),
    )
    row = cursor.fetchone()
    return (row[0], row[1]) if row else None


def set_cursor(cursor, org_id: str, channel_id: str, last_ts: str) -> None:
    cursor.execute(
        """INSERT INTO detection_chat_cursors (org_id, channel_id, last_ts)
           VALUES (%s, %s, %s)
           ON CONFLICT (org_id, channel_id)
           DO UPDATE SET last_ts = EXCLUDED.last_ts, updated_at = NOW()""",
        (org_id, channel_id, last_ts),
    )


def touch_cursor(cursor, org_id: str, channel_id: str) -> None:
    """Move the channel to the back of the queue without advancing it.

    Used when its messages could not be read: they are tried again on its next
    turn, and one unreadable channel does not hold up the others.
    """
    cursor.execute(
        "UPDATE detection_chat_cursors SET updated_at = NOW() WHERE org_id = %s AND channel_id = %s",
        (org_id, channel_id),
    )


def keep_report(cursor, org_id: str, channel_id: str, reading: Reading) -> bool:
    """Store a report once. False when this message was already kept or is not a report."""
    if reading.kind != KIND_REPORT:
        return False
    cursor.execute(
        """INSERT INTO detection_chat_signals
           (org_id, source, channel_id, message_ts, said_at, asset, claim)
           VALUES (%s, %s, %s, %s, to_timestamp(%s), %s, %s)
           ON CONFLICT (org_id, channel_id, message_ts) DO NOTHING""",
        (org_id, SOURCE_SLACK, channel_id, reading.ts, float(reading.ts), reading.asset,
         reading.claim),
    )
    return cursor.rowcount > 0


def run_checks_now(cursor, org_id: str, asset: str) -> int:
    """Make every check on *asset* due. Existing checks answer first, before anything new."""
    cursor.execute(
        """UPDATE detection_checks SET last_run_at = NULL
           WHERE org_id = %s AND lower(asset) = lower(%s) AND state <> %s AND claimed_at IS NULL""",
        (org_id, asset, STATE_RETIRED),
    )
    return cursor.rowcount


def corroborated(cursor, org_id: str, limit: int) -> List[Tuple[str, str, str, str, str, str]]:
    """Open fires of unproven checks that a recent report agrees with.

    [(fire_id, check_id, observed, created_by, signal_id, claim)], oldest fire
    first, one row per fire. A fire that already has a case is not returned.
    """
    cursor.execute(
        """SELECT DISTINCT ON (f.fired_at, f.id)
                  f.id, f.check_id, COALESCE(f.observed, ''), c.created_by, s.id, s.claim
           FROM detection_check_fires f
           JOIN detection_checks c ON c.id = f.check_id
           JOIN detection_chat_signals s
             ON s.org_id = f.org_id AND lower(s.asset) = lower(f.asset)
            AND s.said_at >= f.fired_at - %s * INTERVAL '1 minute'
            AND s.said_at >= NOW() - %s * INTERVAL '1 minute'
           WHERE f.org_id = %s AND c.state = %s AND f.revision = c.revision
             AND f.cleared_at IS NULL AND f.outcome IS NULL AND f.incident_id IS NULL
           ORDER BY f.fired_at, f.id, s.said_at
           LIMIT %s""",
        (CORROBORATION_MINUTES, CORROBORATION_MINUTES, org_id, STATE_SILENT, limit),
    )
    return [(str(r[0]), str(r[1]), r[2], r[3], str(r[4]), r[5]) for r in cursor.fetchall()]


def link_signal_to_case(cursor, signal_id: str, incident_id: str) -> None:
    cursor.execute(
        "UPDATE detection_chat_signals SET incident_id = %s WHERE id = %s", (incident_id, signal_id),
    )


def report_near(cursor, org_id: str, asset: str, fired_at, until, lookback_hours: int) -> Optional[str]:
    """A report about *asset* during a failure, as evidence for grading, or None.

    Returns the first report about that asset on the same day, whichever report
    fell inside the window. Three people saying "checkout is down" during one
    outage are one piece of evidence, and a check that flaps through that
    outage must not collect a separate hit from each of them.
    """
    cursor.execute(
        """SELECT (
               SELECT first.id FROM detection_chat_signals first
               WHERE first.org_id = s.org_id AND lower(first.asset) = lower(s.asset)
                 AND (first.said_at AT TIME ZONE 'UTC')::date = (s.said_at AT TIME ZONE 'UTC')::date
               ORDER BY first.said_at, first.id LIMIT 1
           )
           FROM detection_chat_signals s
           WHERE s.org_id = %s AND lower(s.asset) = lower(%s)
             AND s.said_at >= %s::timestamptz - %s * INTERVAL '1 hour'
             AND s.said_at <= %s::timestamptz
           ORDER BY s.said_at LIMIT 1""",
        (org_id, asset, fired_at, lookback_hours, until),
    )
    row = cursor.fetchone()
    return str(row[0]) if row and row[0] else None
