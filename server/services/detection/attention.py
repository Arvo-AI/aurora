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

One noisy kind of check must not use up the channel for every other kind, so a
kind counts against the budget once however often it was wrong, and after two
posts that were nothing it is not posted again until those age out.

People can answer a post. It carries two buttons: "Not a problem" grades the
failure as false on the spot, and "Stop this check" retires the check.
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from services.detection import STATE_SILENT
from services.detection.cases import healthy_condition, origin_sentence
from services.detection.record import HITS_SQL, OUTCOME_FALSE, OUTCOME_HIT, PatternRecord

logger = logging.getLogger(__name__)

# A failure has to last this long before it is worth anyone's attention.
PERSIST_HOURS = 2
BASE_WEEKLY_BUDGET = 3
MAX_WEEKLY_BUDGET = 10
BUDGET_WINDOW_DAYS = 7
# How long a post's outcome keeps moving the budget.
OUTCOME_MEMORY_DAYS = 90
_CANDIDATE_POOL = 50
# Posts about one kind of check that turned out to be nothing before that kind
# stops being posted.
MUTE_PATTERN_AFTER_FALSE = 2
# A claim with no post recorded after this long belongs to a run that died
# between claiming and sending. It is given back.
STALE_CLAIM_MINUTES = 30
# Recorded as the post's ts when the send failed in transit and may have gone out.
TS_UNKNOWN = "unknown"
# Tries at the "this cleared" thread reply before giving up (archived channel, bot removed).
MAX_CLEAR_ATTEMPTS = 3
# Local hours, on weekdays, in which a post is made. Outside them it waits:
# nobody is paged by these, but a post made at 3am is read by nobody either.
POST_FROM_HOUR, POST_UNTIL_HOUR = 8, 20
ACTION_NOT_A_PROBLEM = "detection_nudge_false"
ACTION_STOP_CHECK = "detection_nudge_stop"


def weekly_budget(real: int, nothing: int) -> int:
    """Posts allowed per week, from how past posts turned out."""
    return max(0, min(MAX_WEEKLY_BUDGET, BASE_WEEKLY_BUDGET + real - nothing))


def channel_budget(cursor, org_id: str, channel: str) -> Tuple[int, int]:
    """(weekly budget, posts already made this week) for *channel*."""
    cursor.execute(
        # Wrong posts are counted by kind of check, not by post: one noisy kind
        # takes one post a week off the channel, not all of them.
        """SELECT COUNT(*) FILTER (WHERE outcome = %s),
                  COUNT(DISTINCT pattern) FILTER (WHERE outcome = %s),
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
                      """ + HITS_SQL + """ AS hits,
                      COUNT(*) FILTER (WHERE outcome = %s) AS false_fires
               FROM detection_check_fires WHERE org_id = %s GROUP BY pattern
           ), muted AS (
               SELECT pattern FROM detection_check_fires
               WHERE org_id = %s AND nudged_at >= NOW() - %s * INTERVAL '1 day'
               GROUP BY pattern
               HAVING COUNT(*) FILTER (WHERE outcome = %s) >= %s
                  AND COUNT(*) FILTER (WHERE outcome = %s) = 0
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
             AND c.pattern NOT IN (SELECT pattern FROM muted)
           ORDER BY COALESCE(p.hits::float / NULLIF(p.hits + p.false_fires, 0), 0) DESC,
                    COALESCE(p.hits, 0) DESC, f.fired_at
           LIMIT %s""",
        (OUTCOME_HIT, OUTCOME_FALSE, org_id,
         org_id, OUTCOME_MEMORY_DAYS, OUTCOME_FALSE, MUTE_PATTERN_AFTER_FALSE, OUTCOME_HIT,
         org_id, STATE_SILENT, PERSIST_HOURS, _CANDIDATE_POOL),
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
    # Eligibility is checked again here, under the write. The candidate list is a
    # snapshot: the check may have passed, been given a case or been graded since,
    # and posting "still failing" about it would be wrong and cost a post.
    cursor.execute(
        """UPDATE detection_check_fires SET nudged_at = NOW(), nudge_channel = %s
           WHERE id = %s AND nudged_at IS NULL
             AND cleared_at IS NULL AND outcome IS NULL AND incident_id IS NULL""",
        (channel, str(fire_id)),
    )
    return cursor.rowcount > 0


def release(cursor, fire_id: str) -> None:
    """Undo a claim whose post did not go out, so it does not use up budget."""
    cursor.execute(
        "UPDATE detection_check_fires SET nudged_at = NULL, nudge_channel = NULL WHERE id = %s",
        (str(fire_id),),
    )


def release_stale_claims(cursor, org_id: str) -> int:
    """Give back claims whose post was never recorded: the run died before sending.

    A send that failed in transit is not one of these. It is recorded with
    TS_UNKNOWN and kept, because the post may be in the channel.
    """
    cursor.execute(
        """UPDATE detection_check_fires SET nudged_at = NULL, nudge_channel = NULL
           WHERE org_id = %s AND nudge_ts IS NULL
             AND nudged_at <= NOW() - %s * INTERVAL '1 minute'""",
        (org_id, STALE_CLAIM_MINUTES),
    )
    return cursor.rowcount


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


# Slack refuses a section whose text is longer than 3000 characters.
_SECTION_MAX_CHARS = 2900
_CUT_ENTITY = re.compile(r"&[a-z]{0,4}$")


def _fit_section(text: str) -> str:
    """*text* short enough for one section, never cut in the middle of an escaped character."""
    if len(text) <= _SECTION_MAX_CHARS:
        return text
    return _CUT_ENTITY.sub("", text[:_SECTION_MAX_CHARS]) + "…"


def worth_a_look_blocks(candidate: Dict[str, Any], budget: int) -> List[Dict[str, Any]]:
    """The post with its two answers. The text is the same one a client without blocks shows."""
    fire_id = str(candidate["fire_id"])
    return [
        {"type": "section", "text": {"type": "mrkdwn",
                                     "text": _fit_section(worth_a_look_message(candidate, budget))}},
        {"type": "actions", "elements": [
            {"type": "button", "action_id": ACTION_NOT_A_PROBLEM, "value": fire_id,
             "text": {"type": "plain_text", "text": "Not a problem"}},
            {"type": "button", "action_id": ACTION_STOP_CHECK, "value": fire_id, "style": "danger",
             "text": {"type": "plain_text", "text": "Stop this check"},
             "confirm": {
                 "title": {"type": "plain_text", "text": "Stop this check?"},
                 "text": {"type": "plain_text",
                          "text": "Aurora stops running it and does not recreate it. Its record is kept."},
                 "confirm": {"type": "plain_text", "text": "Stop it"},
                 "deny": {"type": "plain_text", "text": "Cancel"},
             }},
        ]},
    ]


def answer(cursor, org_id: str, fire_id: str, action_id: str, may_stop: bool) -> Tuple[str, str]:
    """Apply a person's answer to a post. Returns (what was done, the check's name).

    What was done is one of: "false" (graded as nothing), "stopped", "already"
    (someone or something answered first), "not_allowed", "missing".
    """
    from services.detection import record
    from services.detection.store import retire_check_by_id

    cursor.execute(
        """SELECT f.check_id, f.outcome, c.name
           FROM detection_check_fires f JOIN detection_checks c ON c.id = f.check_id
           WHERE f.id = %s AND f.org_id = %s AND f.nudged_at IS NOT NULL FOR UPDATE OF f""",
        (fire_id, org_id),
    )
    row = cursor.fetchone()
    if row is None:
        return "missing", ""
    check_id, outcome, name = str(row[0]), row[1], row[2]
    if action_id == ACTION_STOP_CHECK:
        if not may_stop:
            return "not_allowed", name
        stopped = retire_check_by_id(cursor, org_id, check_id, "Stopped from a worth-a-look post in Slack.")
        return ("stopped" if stopped else "already"), name
    if outcome is not None:
        return "already", name
    # A person saying so is the strongest grade there is: it does not wait for
    # the window to close, and it counts against this version of the check.
    record.apply_outcome(cursor, fire_id, check_id, org_id, OUTCOME_FALSE)
    return "false", name


def working_hours_now(client, slack_user_id: Optional[str], now: Optional[datetime] = None) -> bool:
    """Whether it is a weekday daytime where the workspace's installer is.

    Aurora stores no timezone for an org, so the installer's Slack timezone
    stands in for the team's. When it cannot be read, UTC is used: the weekend
    is still kept clear, and the post still goes out. Holding it until a
    timezone turns up would hold it for good on an install that has none.
    """
    offset = 0
    if slack_user_id:
        try:
            info = client._make_request("GET", "users.info", {"user": slack_user_id}) or {}
            read = (info.get("user") or {}).get("tz_offset")
            if isinstance(read, int) and not isinstance(read, bool):
                offset = read
        except Exception:
            logger.warning("[DetectionAttention] Could not read the installer's timezone", exc_info=True)
    local = (now or datetime.now(timezone.utc)) + timedelta(seconds=offset)
    return local.weekday() < 5 and POST_FROM_HOUR <= local.hour < POST_UNTIL_HOUR


def cleared_to_note(cursor, org_id: str) -> List[Tuple[str, str, str, float]]:
    """Posted fires that have since cleared: [(fire_id, channel, ts, hours failing)]."""
    cursor.execute(
        """SELECT id, nudge_channel, nudge_ts,
                  EXTRACT(EPOCH FROM (cleared_at - fired_at)) / 3600.0
           FROM detection_check_fires
           WHERE org_id = %s AND nudge_ts IS NOT NULL AND nudge_ts <> %s
             AND cleared_at IS NOT NULL AND NOT nudge_clear_noted
             AND nudge_clear_attempts < %s""",
        (org_id, TS_UNKNOWN, MAX_CLEAR_ATTEMPTS),
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
    """The thread reply did not go out: leave it for a later pass, a few times.

    An archived channel or a removed bot refuses every time, so the tries are
    counted and cleared_to_note stops offering it after MAX_CLEAR_ATTEMPTS.
    """
    cursor.execute(
        "UPDATE detection_check_fires SET nudge_clear_noted = FALSE, "
        "nudge_clear_attempts = nudge_clear_attempts + 1 WHERE id = %s",
        (fire_id,),
    )


def cleared_message(hours: float) -> str:
    return "This cleared by itself after %s." % _duration(hours)


def slack_target(user_id: str):
    """(client, incidents channel id, installer's Slack id) for the org.

    (None, None, None) without Slack. The installer id may be None on its own.
    """
    try:
        from connectors.slack_connector.client import get_slack_client_for_user
        from utils.auth.stateless_auth import get_credentials_from_db
        from utils.notifications.slack_notification_service import _get_incidents_channel_id

        client = get_slack_client_for_user(user_id)
        if not client:
            return None, None, None
        channel = _get_incidents_channel_id(user_id, client)
        if not channel:
            return None, None, None
        installer = (get_credentials_from_db(user_id, "slack") or {}).get("user_id")
        return client, channel, installer
    except Exception:
        logger.exception("[DetectionAttention] Could not resolve the Slack channel")
        return None, None, None
