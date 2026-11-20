"""Say so when Aurora can no longer run its checks.

Everything else in detection memory is silent, and that is the problem this
module exists for: if Aurora's access expires, every check errors, nothing
fires, and the quiet looks like health. So this is the one message about
background work Aurora sends on its own. One message per outage, to the person
who connected Slack.
"""

import logging
from typing import List, Optional, Tuple

from services.detection import STATE_RETIRED, STATUS_ERROR
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

# A provider counts as unreachable when every check against it has errored this
# many times in a row. One check erroring is a bad command, not lost access.
BLIND_AFTER_CONSECUTIVE_ERRORS = 3
_DETAIL_MAX_CHARS = 300
# How long a claim may sit unsent before another worker may take it over.
PENDING_CLAIM_SECONDS = 900


def providers_gone_blind(cursor, org_id: str) -> List[Tuple[str, int, str]]:
    """[(provider, checks, latest error detail)] with no open alert yet."""
    cursor.execute(
        """SELECT c.provider, COUNT(*),
                  (SELECT r.detail FROM detection_check_runs r
                   JOIN detection_checks c2 ON c2.id = r.check_id
                   WHERE c2.org_id = %s AND c2.provider = c.provider AND r.status = %s
                   ORDER BY r.created_at DESC LIMIT 1)
           FROM detection_checks c
           WHERE c.org_id = %s AND c.state <> %s
           GROUP BY c.provider
           HAVING MIN(c.consecutive_errors) >= %s
              AND NOT EXISTS (
                  SELECT 1 FROM detection_access_alerts a
                  WHERE a.org_id = %s AND a.provider = c.provider AND a.resolved_at IS NULL
                    AND (a.delivered OR a.alerted_at > NOW() - %s * INTERVAL '1 second')
              )""",
        (org_id, STATUS_ERROR, org_id, STATE_RETIRED, BLIND_AFTER_CONSECUTIVE_ERRORS, org_id,
         PENDING_CLAIM_SECONDS),
    )
    return [(row[0], row[1], row[2] or "") for row in cursor.fetchall()]


def open_alert(cursor, org_id: str, provider: str, detail: str) -> bool:
    """Claim the right to send the message. False when another worker has it.

    The claim is pending until mark_delivered. A pending claim expires: if the
    worker dies between claiming and sending, a later pass takes the claim over
    instead of the outage going unannounced. If it dies after Slack accepted
    the message but before marking it, the message is sent once more; a second
    notice was judged better than none.
    """
    cursor.execute(
        """INSERT INTO detection_access_alerts (org_id, provider, detail)
           VALUES (%s, %s, %s)
           ON CONFLICT (org_id, provider) WHERE resolved_at IS NULL
           DO UPDATE SET alerted_at = NOW(), detail = EXCLUDED.detail
           WHERE NOT detection_access_alerts.delivered
             AND detection_access_alerts.alerted_at <= NOW() - %s * INTERVAL '1 second'""",
        (org_id, provider, detail[:_DETAIL_MAX_CHARS], PENDING_CLAIM_SECONDS),
    )
    return cursor.rowcount > 0


def mark_delivered(cursor, org_id: str, provider: str) -> None:
    cursor.execute(
        "UPDATE detection_access_alerts SET delivered = TRUE "
        "WHERE org_id = %s AND provider = %s AND resolved_at IS NULL",
        (org_id, provider),
    )


def release_alert(cursor, org_id: str, provider: str) -> None:
    """Give up a claim whose message did not go out, so a later pass sends it.

    Without this, one failed send would silence the notice for the whole outage.
    """
    cursor.execute(
        "DELETE FROM detection_access_alerts "
        "WHERE org_id = %s AND provider = %s AND resolved_at IS NULL",
        (org_id, provider),
    )


def resolve_recovered(cursor, org_id: str) -> int:
    """Close alerts for providers where a check is running again."""
    cursor.execute(
        """UPDATE detection_access_alerts a SET resolved_at = NOW()
           WHERE a.org_id = %s AND a.resolved_at IS NULL
             AND EXISTS (
                 SELECT 1 FROM detection_checks c
                 WHERE c.org_id = a.org_id AND c.provider = a.provider
                   AND c.state <> %s AND c.consecutive_errors = 0 AND c.run_count > 0
             )""",
        (org_id, STATE_RETIRED),
    )
    return cursor.rowcount


def blind_message(provider: str, checks: int, detail: str) -> str:
    lines = [
        "I can't reach *%s* any more, so the %d check%s I run there %s stopped working."
        % (provider, checks, "" if checks == 1 else "s", "has" if checks == 1 else "have"),
        "Until this is fixed I won't notice problems there. Quiet from me does not mean healthy.",
    ]
    if detail:
        lines.append("Last error: %s" % detail[:_DETAIL_MAX_CHARS])
    lines.append("Reconnecting %s in Aurora's connectors should fix it." % provider)
    return "\n".join(lines)


def tell_installer(user_id: str, provider: str, checks: int, detail: str) -> Optional[str]:
    """Message whoever connected Slack. Returns the Slack id messaged, or None.

    *user_id* is any user of the org: the Slack bot token and the installer's
    Slack id are stored once per org.
    """
    try:
        from connectors.slack_connector.client import get_slack_client_for_user
        from utils.auth.stateless_auth import get_credentials_from_db

        client = get_slack_client_for_user(user_id)
        credentials = get_credentials_from_db(user_id, "slack") or {}
        installer = credentials.get("user_id")
        if not client or not installer:
            return None
        client.send_message(channel=installer, text=blind_message(provider, checks, detail))
        return installer
    except Exception:
        logger.exception("[DetectionAccess] Could not send the lost-access message for %s",
                         sanitize(provider))
        return None
