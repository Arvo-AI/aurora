"""Run detection checks on a schedule, with no model in the loop.

Scheduled actions (services/actions) always start an agent chat session. A check
is a saved command, so it runs here as plain code: the tick claims what is due,
one task per check executes it, and the result is folded into the check's record.

Three slower tasks keep the store honest: grading decides which fires were real
and moves checks between silent and proven, the access task says so when Aurora
can no longer run its checks, and consolidation retires what earns nothing.

A silent check's failure is a row in the database. Only a proven check's
failure opens a case.
"""

import logging
import os
import time
from contextlib import contextmanager

from celery_config import celery_app
from services.detection import STATE_PROVEN, STATE_RETIRED, STATUS_ERROR
from services.detection import (
    access,
    attention,
    cases,
    chat_reader,
    chat_signals,
    consolidation,
    ownership,
    record,
    replay,
)
from services.detection.assertion import Verdict
from services.detection.execution import judge
from services.detection.store import (
    claim_due_checks,
    fires_awaiting_a_case,
    get_check_for_case,
    get_runnable_check,
    link_fire_to_case,
    record_result,
    reserve_case,
)
from utils.auth.stateless_auth import set_rls_context
from utils.db.connection_pool import db_pool
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

# Ceiling on checks started per org per tick, so a large store that all comes
# due at once spreads over the following ticks instead of flooding the workers.
MAX_CHECKS_PER_ORG_PER_TICK = 20
# Cases retried per org per tick. The RCA path has its own rate limit; this
# only bounds how often it is asked.
MAX_CASE_RETRIES_PER_ORG_PER_TICK = 3
_LOG = "[DetectionRunner]"


def checks_enabled() -> bool:
    """Off switch for the runner (DETECTION_CHECKS_ENABLED=false)."""
    return (os.getenv("DETECTION_CHECKS_ENABLED") or "true").strip().lower() != "false"


def chat_intake_enabled() -> bool:
    """Reading chat channels is opt-in (DETECTION_CHAT_INTAKE_ENABLED=true).

    It sends every message of the channels Aurora is in through a model, which
    is a decision for whoever runs the install, not a default.
    """
    return (os.getenv("DETECTION_CHAT_INTAKE_ENABLED") or "false").strip().lower() == "true"


def _org_representatives():
    """[(user_id, org_id)], one user per org. `users` is not RLS-protected."""
    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT ON (org_id) id, org_id "
            "FROM users WHERE org_id IS NOT NULL "
            "ORDER BY org_id, id"
        )
        return cur.fetchall()


@contextmanager
def _org_cursor(user_id: str, org_id: str):
    """Yield (cursor, conn) scoped to *org_id*, or (None, None) when it cannot be.

    *org_id* is pinned rather than resolved from the user, so work for a check
    whose creator has left the org is still recorded.
    """
    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        if set_rls_context(cur, conn, user_id, org_id=org_id, log_prefix=_LOG):
            yield cur, conn
        else:
            yield None, None


def _for_each_org(work, what: str) -> None:
    """Run work(cursor, conn, user_id, org_id) per org; one org failing skips only itself."""
    try:
        representatives = _org_representatives()
    except Exception:
        logger.exception("%s Failed to list orgs for %s", _LOG, what)
        return
    for user_id, org_id in representatives:
        try:
            with _org_cursor(user_id, org_id) as (cur, conn):
                if cur is not None:
                    work(cur, conn, user_id, org_id)
        except Exception:
            logger.exception("%s %s failed for org %s", _LOG, what, sanitize(org_id))


@celery_app.task(name="services.detection.runner.run_due_checks")
def run_due_checks():
    """Claim every due check and queue one run each; retry cases that did not open."""
    if not checks_enabled():
        return

    claimed, awaiting = [], []

    def _claim(cur, conn, _user_id, org_id):
        # Before claiming: a check claimed under a creator who has left would
        # run, and fail, as that creator.
        ownership.reassign_orphaned_checks(cur, org_id)
        due = claim_due_checks(cur, org_id, MAX_CHECKS_PER_ORG_PER_TICK)
        conn.commit()
        claimed.extend((check_id, org_id, created_by, claim) for check_id, created_by, claim in due)
        awaiting.extend(
            (fire_id, check_id, org_id, created_by, observed, None)
            for fire_id, check_id, observed, created_by
            in fires_awaiting_a_case(cur, org_id, MAX_CASE_RETRIES_PER_ORG_PER_TICK)
        )
        # An unproven check failing where a person just reported a problem.
        awaiting.extend(
            (fire_id, check_id, org_id, created_by, observed, (signal_id, claim))
            for fire_id, check_id, observed, created_by, signal_id, claim
            in chat_signals.corroborated(cur, org_id, MAX_CASE_RETRIES_PER_ORG_PER_TICK)
        )

    _for_each_org(_claim, "claiming due checks")

    for check_id, org_id, created_by, claim in claimed:
        try:
            run_check.delay(check_id, org_id, created_by, claim)
        except Exception:
            # The claim expires by itself, so the check is picked up again later.
            logger.exception("%s Failed to queue check %s", _LOG, sanitize(check_id))

    for fire_id, check_id, org_id, created_by, observed, report in awaiting:
        _open_or_join_case(fire_id, check_id, org_id, created_by, observed, report)


def _open_or_join_case(fire_id: str, check_id: str, org_id: str, user_id: str, observed: str,
                       report: tuple | None = None) -> None:
    """Give a fire its case: join a fresh one on the asset, or open one.

    A proven check earns a case by failing. An unproven one only gets a case
    when *report* (signal_id, claim) says a person reported the same asset.
    """
    try:
        with _org_cursor(user_id, org_id) as (cur, conn):
            if cur is None:
                return
            # The incident is created as *user_id*. A creator who has moved to
            # another org would carry this org's command and output with them,
            # so the case waits until the check has an owner here. Read from
            # the table, not the membership cache; open_case then has the
            # incident refused if it would land anywhere but this org.
            cur.execute("SELECT org_id FROM users WHERE id = %s", (user_id,))
            owner = cur.fetchone()
            if owner is None or owner[0] != org_id:
                logger.warning("%s Check %s has no owner in its org; case not opened",
                               _LOG, sanitize(check_id))
                return
            check = get_check_for_case(cur, check_id)
            # A retired check gets no case, report or not: retiring leaves its
            # open fire in place, and a report must not bring it back to life.
            if check is None or check["state"] == STATE_RETIRED:
                return
            if check["state"] != STATE_PROVEN and report is None:
                return
            # Re-checked under the write: the fire may have cleared or been given
            # a case since the tick selected it, and another worker may hold it.
            reserved = reserve_case(cur, fire_id, allow_graded=report is not None)
            existing = cases.find_open_case(cur, org_id, check["asset"]) if reserved else None
            if existing:
                _link(cur, fire_id, existing, False, report)
            conn.commit()
            if not reserved or existing:
                return

        incident_id = cases.open_case(
            check, observed, user_id, org_id=org_id, fire_id=fire_id,
            reported=report[1] if report else "",
        )
        if not incident_id:
            return
        with _org_cursor(user_id, org_id) as (cur, conn):
            if cur is None:
                return
            _link(cur, fire_id, incident_id, True, report)
            conn.commit()
    except Exception:
        logger.exception("%s Failed to open a case for check %s", _LOG, sanitize(check_id))


def _link(cur, fire_id: str, incident_id: str, opened: bool, report: tuple | None) -> None:
    link_fire_to_case(cur, fire_id, incident_id, opened=opened)
    if report:
        chat_signals.link_signal_to_case(cur, report[0], incident_id)


@celery_app.task(name="services.detection.runner.run_check")
def run_check(check_id: str, org_id: str, user_id: str, claim: str | None = None):
    """Execute one claimed check, record the result, and open a case if it earned one."""
    # Checked here too: runs queued before the switch was turned off must not
    # execute commands after it.
    if not checks_enabled():
        return
    started = time.monotonic()
    try:
        with _org_cursor(user_id, org_id) as (cur, _conn):
            check = get_runnable_check(cur, check_id) if cur is not None else None
            owner_org = None
            if check is not None:
                cur.execute("SELECT org_id FROM users WHERE id = %s", (check["created_by"],))
                owner = cur.fetchone()
                owner_org = owner[0] if owner else None
        if check is None:
            return

        # The owner as of this read, from the same row as the definition: a
        # revision saved while the run was queued changes both together.
        user_id = check["created_by"]
        if owner_org != org_id:
            # Credentials and command policy are resolved from the user's
            # current org. An owner who has moved would run this org's check
            # with another org's access and write the output back here.
            verdict = Verdict(STATUS_ERROR, detail="the check's owner is no longer in this organization")
        else:
            verdict = judge(check, user_id, check_id)
        duration_ms = int((time.monotonic() - started) * 1000)

        with _org_cursor(user_id, org_id) as (cur, conn):
            if cur is None:
                return
            recorded = record_result(
                cur, check_id, org_id, verdict, duration_ms,
                revision=check["revision"], claim=claim,
            )
            conn.commit()
    except Exception:
        logger.exception("%s Failed to run check %s", _LOG, sanitize(check_id))
        return

    if recorded.fire_id and recorded.state == STATE_PROVEN:
        _open_or_join_case(recorded.fire_id, check_id, org_id, user_id, verdict.observed)


@celery_app.task(name="services.detection.runner.grade_fires")
def grade_fires():
    """Decide which fires were real, and move checks between silent and proven."""
    if not checks_enabled():
        return

    def _grade(cur, conn, _user_id, org_id):
        hits, false_fires = record.grade_fires(cur, org_id)
        conn.commit()
        if hits or false_fires:
            logger.info("%s Graded fires for org %s: %d hit(s), %d false",
                        _LOG, sanitize(org_id), hits, false_fires)

    _for_each_org(_grade, "grading fires")


@celery_app.task(name="services.detection.runner.watch_access")
def watch_access():
    """Tell the installer, once per outage, when Aurora cannot run its checks."""
    if not checks_enabled():
        return

    def _watch(cur, conn, user_id, org_id):
        access.resolve_recovered(cur, org_id)
        conn.commit()
        for provider, checks, detail in access.providers_gone_blind(cur, org_id):
            # Claim first and commit: two workers must not both send the message.
            claim = access.open_alert(cur, org_id, provider, detail)
            conn.commit()
            if not claim:
                continue
            if access.tell_installer(user_id, provider, checks, detail):
                access.mark_delivered(cur, org_id, provider, claim)
            else:
                # Not delivered (no Slack, send failed): let a later pass try again.
                access.release_alert(cur, org_id, provider, claim)
            conn.commit()

    _for_each_org(_watch, "watching access")


@celery_app.task(name="services.detection.runner.consolidate_checks")
def consolidate_checks():
    """Retire duplicates, checks that were only ever wrong, and the weakest past the ceiling."""
    if not checks_enabled():
        return

    def _consolidate(cur, conn, _user_id, org_id):
        retired = consolidation.consolidate(cur, org_id)
        conn.commit()
        if any(retired.values()):
            logger.info("%s Consolidated org %s: %s", _LOG, sanitize(org_id), retired)

    _for_each_org(_consolidate, "consolidating checks")


@celery_app.task(name="services.detection.runner.spend_attention")
def spend_attention():
    """Post "worth a look" for long failures of unproven checks, within the weekly budget."""
    if not checks_enabled():
        return

    def _spend(cur, conn, user_id, org_id):
        waiting = attention.candidates(cur, org_id)
        cleared = attention.cleared_to_note(cur, org_id)
        if not waiting and not cleared:
            return
        client, channel = attention.slack_target(user_id)
        if client is None:
            return

        for fire_id, posted_channel, ts, hours in cleared:
            # Marked first, so a crash after sending cannot say it twice.
            noted = attention.mark_clear_noted(cur, fire_id)
            conn.commit()
            if noted:
                _sent, refused = _send(client, posted_channel, attention.cleared_message(hours), thread_ts=ts)
                if refused:
                    attention.unmark_clear_noted(cur, fire_id)
                    conn.commit()

        budget, spent = attention.channel_budget(cur, org_id, channel)
        for candidate in waiting[:max(0, budget - spent)]:
            # Claimed and committed before sending: two workers must not both post.
            # The claim re-reads the budget under a lock; the slice above only
            # avoids asking for more than could possibly be granted.
            claimed = attention.claim(cur, org_id, candidate["fire_id"], channel)
            conn.commit()
            if not claimed:
                continue
            ts, refused = _send(client, channel, attention.worth_a_look_message(candidate, budget))
            if ts:
                attention.record_post(cur, candidate["fire_id"], ts)
            elif refused:
                attention.release(cur, candidate["fire_id"])
            # Neither: the outcome is unknown and the claim is kept. See _send.
            conn.commit()

    _for_each_org(_spend, "spending attention")


def _send(client, channel: str, text: str, thread_ts: str | None = None) -> tuple[str | None, bool]:
    """Post to Slack. Returns (message ts, whether Slack refused it).

    Three outcomes, and they are not the same:
    - (ts, False): posted.
    - (None, True): Slack answered and said no. Nothing was posted, so the
      caller may give the post back to be tried again.
    - (None, False): the request failed in transit. Slack may have accepted it
      before the connection dropped, so the caller must not try again: a second
      attempt could post the same thing twice. One possibly-missing post was
      judged better than a possible duplicate in a tier that promises to be quiet.
    """
    from connectors.slack_connector.client import SlackAPIError

    try:
        result = client.send_message(channel=channel, text=text, thread_ts=thread_ts)
    except SlackAPIError:
        logger.warning("%s Slack refused a post", _LOG, exc_info=True)
        return None, True
    except Exception:
        logger.exception("%s Slack post failed in transit", _LOG)
        return None, False
    ts = (result or {}).get("ts")
    return (ts, False) if ts else (None, True)


@celery_app.task(name="services.detection.runner.run_fire_drills")
def run_fire_drills():
    """Re-run the replay that admitted each proven check; demote one that now misses.

    A check can stop catching its own incident without anyone revising it: a
    log format changes, a metric is renamed. The replay that let it in is the
    test that says so.
    """
    if not checks_enabled():
        return

    targets = []

    def _collect(cur, _conn, user_id, org_id):
        targets.extend((org_id, user_id, target) for target in replay.drill_targets(cur, org_id))

    _for_each_org(_collect, "collecting fire drills")

    for org_id, user_id, target in targets:
        try:
            if replay.still_catches(target) is not False:
                continue
            with _org_cursor(user_id, org_id) as (cur, conn):
                if cur is None:
                    continue
                replay.fail_drill(
                    cur, str(target["id"]), org_id, target["revision"],
                    "Fire drill: replayed over incident %s, this check no longer fails."
                    % target["replay"].get("incident_id"),
                )
                conn.commit()
            logger.info("%s Fire drill demoted check %s", _LOG, sanitize(str(target["id"])))
        except Exception:
            logger.exception("%s Fire drill failed for check %s", _LOG, sanitize(str(target["id"])))


_HISTORY_LIMIT = 15
# Pages read per turn when a channel has more new messages than one page holds.
_HISTORY_MAX_PAGES = 4


def _new_messages(client, channel_id: str, last_ts: str):
    """Messages after *last_ts*: (messages, whether that is all of them).

    Slack returns the newest page first, so stopping at one page would move the
    cursor past older messages that were never read. This follows the pages
    back to the cursor, up to a bound.
    """
    messages, page_cursor = [], None
    for _ in range(_HISTORY_MAX_PAGES):
        params = {"channel": channel_id, "oldest": last_ts, "limit": _HISTORY_LIMIT}
        if page_cursor:
            params["cursor"] = page_cursor
        result = client._make_request("GET", "conversations.history", params) or {}
        messages.extend(m for m in result.get("messages", []) if m.get("ts"))
        page_cursor = (result.get("response_metadata") or {}).get("next_cursor")
        if not result.get("has_more") or not page_cursor:
            return messages, True
    return messages, False


@celery_app.task(name="services.detection.runner.read_chat")
def read_chat():
    """Read one channel per org for problem reports. Opt-in; posts nothing."""
    if not checks_enabled() or not chat_intake_enabled():
        return
    try:
        representatives = _org_representatives()
    except Exception:
        logger.exception("%s Failed to list orgs for reading chat", _LOG)
        return
    for user_id, org_id in representatives:
        try:
            _read_one_channel(user_id, org_id)
        except Exception:
            logger.exception("%s Reading chat failed for org %s", _LOG, sanitize(org_id))


def _read_one_channel(user_id: str, org_id: str) -> None:
    """Three steps, so no database connection is held while Slack or the model is called."""
    with _org_cursor(user_id, org_id) as (cur, conn):
        if cur is None:
            return
        turn = chat_signals.next_channel(cur, org_id)
        if turn is None:
            return
        channel_id, last_ts = turn
        if last_ts is None:
            # First sight of a channel: start from now. Its history is not read.
            chat_signals.set_cursor(cur, org_id, channel_id, "%.6f" % time.time())
            conn.commit()
            return
        assets = chat_signals.known_assets(cur, org_id)

    from connectors.slack_connector.client import get_slack_client_for_user
    client = get_slack_client_for_user(user_id)
    if client is None:
        return

    readings, newest_ts = None, last_ts
    try:
        messages, complete = _new_messages(client, channel_id, last_ts)
        if not complete:
            # More arrived since the last turn than one turn reads. The oldest
            # are skipped, and said so here: Slack returns newest first, so the
            # only way to reach them is to page through everything newer.
            logger.warning("%s Channel %s had more than %d new messages; the oldest were not read",
                           _LOG, sanitize(channel_id), _HISTORY_LIMIT * _HISTORY_MAX_PAGES)
        if messages:
            newest_ts = max((m["ts"] for m in messages), key=float)
        readings = chat_reader.read(
            [m for m in messages if chat_reader.is_human_message(m)], assets, user_id,
        )
    except Exception:
        logger.exception("%s Could not read channel %s", _LOG, sanitize(channel_id))

    with _org_cursor(user_id, org_id) as (cur, conn):
        if cur is None:
            return
        if readings is None:
            chat_signals.touch_cursor(cur, org_id, channel_id)
        else:
            for reading in readings:
                if chat_signals.keep_report(cur, org_id, channel_id, reading) and reading.asset:
                    chat_signals.run_checks_now(cur, org_id, reading.asset)
            chat_signals.set_cursor(cur, org_id, channel_id, newest_ts)
        conn.commit()
