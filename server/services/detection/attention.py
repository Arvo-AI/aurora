"""The second tier: "worth a look", on a budget.

A proven check that fails opens a case, and that is never limited. This module
is for the weaker signal: a check that has not earned that right, but has been
failing for hours. Aurora may mention it once, in the incidents channel, with
no @mention and no investigation.

"Not chatty" is a number here. Each channel has a small weekly budget of these
posts. When more failures qualify than the budget allows, the ones with the
strongest record go out and the rest stay in the log. The budget moves with
what the posts turned out to be: one that pointed at a real incident raises
it, one that pointed at nothing lowers it, down to zero. Old outcomes age out,
so a channel that reached zero gets another chance later.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from services.detection import STATE_SILENT
from services.detection.cases import healthy_condition, origin_sentence
from services.detection.record import OUTCOME_FALSE, OUTCOME_HIT, PatternRecord

logger = logging.getLogger(__name__)

# A failure has to last this long before it is worth anyone's attention.
PERSIST_HOURS = 2
BASE_WEEKLY_BUDGET = 3
MAX_WEEKLY_BUDGET = 10
BUDGET_WINDOW_DAYS = 7
# How long a post's outcome keeps moving the budget.
OUTCOME_MEMORY_DAYS = 90
_CANDIDATE_POOL = 50


def weekly_budget(real: int, nothing: int) -> int:
    """Posts allowed per week, from how past posts turned out."""
    return max(0, min(MAX_WEEKLY_BUDGET, BASE_WEEKLY_BUDGET + real - nothing))


def channel_budget(cursor, org_id: str, channel: str) -> Tuple[int, int]:
    """(weekly budget, posts already made this week) for *channel*."""
    cursor.execute(
        """SELECT COUNT(*) FILTER (WHERE outcome = %s),
                  COUNT(*) FILTER (WHERE outcome = %s),
                  COUNT(*) FILTER (WHERE nudged_at >= NOW() - %s * INTERVAL '1 day')
           FROM detection_check_fires
           WHERE org_id = %s AND nudge_channel = %s
             AND nudged_at >= NOW() - %s * INTERVAL '1 day'""",
        (OUTCOME_HIT, OUTCOME_FALSE, BUDGET_WINDOW_DAYS, org_id, channel, OUTCOME_MEMORY_DAYS),
    )
    real, nothing, spent = cursor.fetchone()
    return weekly_budget(real or 0, nothing or 0), spent or 0


_CANDIDATE_COLUMNS = (
    "fire_id", "check_id", "fired_at", "observed", "name", "asset", "pattern", "prediction",
    "assertion", "origin", "hours_failing",
)


def strongest_first(candidates: List[Dict[str, Any]], records: Dict[str, PatternRecord]) -> List[Dict[str, Any]]:
    """Order by how much the failure deserves attention.

    A kind of check that has been right before outranks one with no record, and
    among equals the failure that has lasted longest goes first. This is the
    rule the query in candidates() applies; it is kept as code so it can be
    tested without a database.
    """
    def rank(candidate):
        record = records.get(candidate["pattern"], PatternRecord())
        return (-record.precision, -record.hits, candidate["fired_at"])
    return sorted(candidates, key=rank)


def candidates(cursor, org_id: str) -> List[Dict[str, Any]]:
    """Open failures of silent checks that have lasted long enough, strongest first.

    Ranked in the query, before the limit: ranking a limited page afterwards
    could leave out the one failure whose kind of check has a record, behind
    fifty older ones that have none. A fire that already belongs to a case is
    excluded: people have that thread.
    """
    cursor.execute(
        """WITH pattern_records AS (
               SELECT pattern,
                      COUNT(DISTINCT evidence_incident_id) FILTER (WHERE outcome = %s) AS hits,
                      COUNT(*) FILTER (WHERE outcome = %s) AS false_fires
               FROM detection_check_fires WHERE org_id = %s GROUP BY pattern
           )
           SELECT f.id, f.check_id, f.fired_at, COALESCE(f.observed, ''), c.name, c.asset,
                  c.pattern, c.prediction, c.assertion, c.origin,
                  EXTRACT(EPOCH FROM (NOW() - f.fired_at)) / 3600.0
           FROM detection_check_fires f
           JOIN detection_checks c ON c.id = f.check_id
           LEFT JOIN pattern_records p ON p.pattern = c.pattern
           WHERE f.org_id = %s AND c.state = %s AND f.revision = c.revision
             AND f.cleared_at IS NULL AND f.outcome IS NULL
             AND f.incident_id IS NULL AND f.nudged_at IS NULL
             AND f.fired_at <= NOW() - %s * INTERVAL '1 hour'
           ORDER BY COALESCE(p.hits::float / NULLIF(p.hits + p.false_fires, 0), 0) DESC,
                    COALESCE(p.hits, 0) DESC, f.fired_at
           LIMIT %s""",
        (OUTCOME_HIT, OUTCOME_FALSE, org_id, org_id, STATE_SILENT, PERSIST_HOURS, _CANDIDATE_POOL),
    )
    return [dict(zip(_CANDIDATE_COLUMNS, row)) for row in cursor.fetchall()]


def claim(cursor, org_id: str, fire_id: str, channel: str) -> bool:
    """Take one post out of the channel's budget for this fire. False when there is none left.

    The budget is read and the post is counted in one transaction, behind a
    lock on the channel that lasts until the caller commits. Two overlapping
    runs therefore cannot both see the last free post and both spend it. The
    per-fire condition alone only stops one fire being posted twice.
    """
    cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("detection-attention:%s:%s" % (org_id, channel),))
    budget, spent = channel_budget(cursor, org_id, channel)
    if spent >= budget:
        return False
    cursor.execute(
        "UPDATE detection_check_fires SET nudged_at = NOW(), nudge_channel = %s "
        "WHERE id = %s AND nudged_at IS NULL",
        (channel, str(fire_id)),
    )
    return cursor.rowcount > 0


def release(cursor, fire_id: str) -> None:
    """Undo a claim whose post did not go out, so it does not use up budget."""
    cursor.execute(
        "UPDATE detection_check_fires SET nudged_at = NULL, nudge_channel = NULL WHERE id = %s",
        (str(fire_id),),
    )


def record_post(cursor, fire_id: str, message_ts: Optional[str]) -> None:
    cursor.execute(
        "UPDATE detection_check_fires SET nudge_ts = %s WHERE id = %s", (message_ts, str(fire_id)),
    )


def slack_text(value: Any) -> str:
    """*value* as inert Slack text.

    Names, predictions and command output come from a model or from a
    customer's systems. Unescaped, `<!channel>` or `<@U123>` inside them would
    notify people, which is exactly what this tier promises not to do.
    """
    text = str(value if value is not None else "")
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("`", "'")


def _duration(hours: float) -> str:
    minutes = int(round(hours * 60))
    if minutes < 60:
        return "%dm" % minutes
    return "%dh %02dm" % divmod(minutes, 60)


def worth_a_look_message(candidate: Dict[str, Any], budget: int) -> str:
    """The post. Deterministic: no model writes or decides it."""
    name, asset = slack_text(candidate["name"]), slack_text(candidate["asset"])
    return "\n".join((
        "*Worth a look:* my check `%s` on *%s* has been failing for %s."
        % (name, asset, _duration(candidate["hours_failing"])),
        "It predicts: %s" % slack_text(candidate["prediction"]),
        "Healthy when: %s" % slack_text(healthy_condition(candidate["assertion"])),
        "Value seen: %s" % (slack_text(candidate["observed"]) or "(empty)"),
        "Why I watch this: %s." % slack_text(origin_sentence(candidate["origin"])),
        "I have not opened an incident, because this check has not proven itself yet. "
        "I post at most %d of these a week here, and fewer when they turn out to be nothing." % budget,
    ))


def cleared_to_note(cursor, org_id: str) -> List[Tuple[str, str, str, float]]:
    """Posted fires that have since cleared: [(fire_id, channel, ts, hours failing)]."""
    cursor.execute(
        """SELECT id, nudge_channel, nudge_ts,
                  EXTRACT(EPOCH FROM (cleared_at - fired_at)) / 3600.0
           FROM detection_check_fires
           WHERE org_id = %s AND nudge_ts IS NOT NULL
             AND cleared_at IS NOT NULL AND NOT nudge_clear_noted""",
        (org_id,),
    )
    return [(str(r[0]), r[1], r[2], float(r[3])) for r in cursor.fetchall()]


def mark_clear_noted(cursor, fire_id: str) -> bool:
    cursor.execute(
        "UPDATE detection_check_fires SET nudge_clear_noted = TRUE "
        "WHERE id = %s AND NOT nudge_clear_noted",
        (fire_id,),
    )
    return cursor.rowcount > 0


def unmark_clear_noted(cursor, fire_id: str) -> None:
    """The thread reply did not go out: leave it to be sent on a later pass."""
    cursor.execute(
        "UPDATE detection_check_fires SET nudge_clear_noted = FALSE WHERE id = %s", (fire_id,),
    )


def cleared_message(hours: float) -> str:
    return "This cleared by itself after %s." % _duration(hours)


def slack_target(user_id: str):
    """(client, incidents channel id) for the org, or (None, None) without Slack."""
    try:
        from connectors.slack_connector.client import get_slack_client_for_user
        from utils.notifications.slack_notification_service import _get_incidents_channel_id

        client = get_slack_client_for_user(user_id)
        if not client:
            return None, None
        channel = _get_incidents_channel_id(user_id, client)
        return (client, channel) if channel else (None, None)
    except Exception:
        logger.exception("[DetectionAttention] Could not resolve the Slack channel")
        return None, None
