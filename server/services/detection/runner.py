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
from services.detection import STATE_PROVEN
from services.detection import access, cases, consolidation, record
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
from utils.auth.stateless_auth import get_org_id_for_user, set_rls_context
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
        due = claim_due_checks(cur, org_id, MAX_CHECKS_PER_ORG_PER_TICK)
        conn.commit()
        claimed.extend((check_id, org_id, created_by, claim) for check_id, created_by, claim in due)
        awaiting.extend(
            (fire_id, check_id, org_id, created_by, observed)
            for fire_id, check_id, observed, created_by
            in fires_awaiting_a_case(cur, org_id, MAX_CASE_RETRIES_PER_ORG_PER_TICK)
        )

    _for_each_org(_claim, "claiming due checks")

    for check_id, org_id, created_by, claim in claimed:
        try:
            run_check.delay(check_id, org_id, created_by, claim)
        except Exception:
            # The claim expires by itself, so the check is picked up again later.
            logger.exception("%s Failed to queue check %s", _LOG, sanitize(check_id))

    for fire_id, check_id, org_id, created_by, observed in awaiting:
        _open_or_join_case(fire_id, check_id, org_id, created_by, observed)


def _open_or_join_case(fire_id: str, check_id: str, org_id: str, user_id: str, observed: str) -> None:
    """Give a proven check's fire its case: join a fresh one on the asset, or open one."""
    # The incident is created as *user_id*, in whatever org that user is in now.
    # A creator who has moved would carry this org's command and output into
    # another org's incident, so the case waits until the check has an owner here.
    if get_org_id_for_user(user_id) != org_id:
        logger.warning("%s Check %s has no owner in its org; case not opened", _LOG, sanitize(check_id))
        return
    try:
        with _org_cursor(user_id, org_id) as (cur, conn):
            if cur is None:
                return
            check = get_check_for_case(cur, check_id)
            if check is None or check["state"] != STATE_PROVEN:
                return
            # Re-checked under the write: the fire may have cleared or been given
            # a case since the tick selected it, and another worker may hold it.
            reserved = reserve_case(cur, fire_id)
            existing = cases.find_open_case(cur, org_id, check["asset"]) if reserved else None
            if existing:
                link_fire_to_case(cur, fire_id, existing, opened=False)
            conn.commit()
            if not reserved or existing:
                return

        incident_id = cases.open_case(check, observed, user_id)
        if not incident_id:
            return
        with _org_cursor(user_id, org_id) as (cur, conn):
            if cur is None:
                return
            link_fire_to_case(cur, fire_id, incident_id, opened=True)
            conn.commit()
    except Exception:
        logger.exception("%s Failed to open a case for check %s", _LOG, sanitize(check_id))


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
        if check is None:
            return

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
            claimed = access.open_alert(cur, org_id, provider, detail)
            conn.commit()
            if claimed and not access.tell_installer(user_id, provider, checks, detail):
                # Not delivered (no Slack, send failed): let a later pass try again.
                access.release_alert(cur, org_id, provider)
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
