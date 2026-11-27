"""Start alert-driven RCAs without ever dropping one.

Every webhook that creates an incident hands its RCA to ``dispatch_alert_rca``.
It starts the investigation now when the org has budget for it, and otherwise
queues it on the incident (``aurora_status = 'queued'``) with the reason and the
prompt it would have run. A periodic drain starts queued investigations, one
RCA per incident, most severe first and then oldest first, as budget frees up.
Nothing is lost: a queued incident is shown as "Not investigated yet" with a
reason, and can be started by hand.

Related alerts are folded into one incident at ingest by the alert correlator
(services/correlation), before they get here, so the queue does not group.

The budget is per org and covers alert-driven RCAs only. Housekeeping runs
(actions, team routing, prediscovery, chat-triggered RCAs) keep their own
per-user limiter in ``task.is_background_chat_allowed`` and never spend it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from celery_config import celery_app
from utils.auth.stateless_auth import set_rls_context
from utils.db.connection_pool import db_pool
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

# --- Budget, per org, alert-driven RCAs only ------------------------------------
# The first limit is the one alerts always had (5 per user per 5 minutes), so
# an org that never hit it sees no change. The other two only bind on a burst
# or a sustained flood.
WINDOW_SECONDS = 300
MAX_PER_WINDOW = 5
MAX_PER_HOUR = 30
MAX_CONCURRENT = 6
# A dispatched RCA no worker has picked up yet still counts as in flight for
# this long; after that the stale sweep owns it.
_DISPATCH_PICKUP_MINUTES = 30
# A running RCA is bounded by the investigation time limit; anything older is
# a leftover the sweep will fail, not load.
_RUNNING_MAX_HOURS = 3
# A delivery retried this soon after its RCA was started does not start another.
_REDISPATCH_GUARD_SECONDS = 120
# Bound on investigations one drain pass starts per org.
_MAX_DISPATCHES_PER_PASS = 10
# Bound on failed starts one drain pass tolerates per org before giving up
# until the next tick.
_MAX_FAILED_PER_PASS = 25

QUEUED = "queued"

# Why an incident is not investigated yet. Codes are stored in
# incidents.rca_queue_reason and mapped to copy by the client.
REASON_RATE = "rate_limited"
REASON_CONCURRENCY = "concurrency"
REASON_HOURLY = "hourly_limit"
# Others were already waiting: this one takes its turn behind them.
REASON_WAITING = "waiting_turn"
REASON_DISPATCH_FAILED = "dispatch_failed"
QUEUE_REASONS = frozenset({REASON_RATE, REASON_CONCURRENCY, REASON_HOURLY, REASON_WAITING, REASON_DISPATCH_FAILED})

# Drain order: most severe first, then oldest. Mirrors _SEVERITY_ORDER_SQL.
_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
_SEVERITY_ORDER_SQL = (
    "CASE lower(COALESCE(severity, '')) WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
    "WHEN 'medium' THEN 2 WHEN 'low' THEN 3 WHEN 'info' THEN 4 ELSE 5 END"
)
# Which queued incidents the drain starts: any not merged away. A re-delivered
# alert can queue an incident that is already 'analyzed' (a new RCA for an old
# incident); it drains like any other, so the queue and the drain share this.
_DRAINABLE_SQL = "status != 'merged'"

DISPATCHED = "dispatched"
QUEUED_OUTCOME = "queued"
# The incident is not visible (deleted, wrong org): retrying cannot help.
MISSING = "missing"
# A temporary error (database, lock timeout) stopped the dispatch before the
# RCA was started or queued: the caller should retry the dispatch.
FAILED = "failed"
# The incident failed to start MAX_START_ATTEMPTS times in a row and is no
# longer retried; it shows as an error. Retrying the delivery cannot help.
ABANDONED = "abandoned"

# Failed starts in a row (e.g. an owner who left the org) before the queue
# gives up on an incident instead of retrying it every minute.
MAX_START_ATTEMPTS = 5
# An RCA already running for the incident: a re-fired alert does not start another.
_IN_FLIGHT_STATES = ("running", "summarizing")


@dataclass
class RcaRequest:
    """Everything needed to start the RCA later exactly as it would have run now."""

    source: str
    prompt: str
    rail_text: Optional[str]
    session_title: str
    trigger_metadata: Dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str)

    @classmethod
    def from_db(cls, value: Any) -> Optional["RcaRequest"]:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                return None
        if not isinstance(value, dict) or not value.get("prompt"):
            return None
        return cls(
            source=str(value.get("source") or ""),
            prompt=str(value["prompt"]),
            rail_text=value.get("rail_text"),
            session_title=str(value.get("session_title") or "RCA"),
            trigger_metadata=value.get("trigger_metadata") if isinstance(value.get("trigger_metadata"), dict) else {},
        )


@dataclass
class QueuedIncident:
    id: str
    user_id: str
    source_type: str
    severity: Optional[str]
    queued_at: datetime
    request: RcaRequest


@dataclass
class BudgetUsage:
    started_in_window: int
    started_in_hour: int
    in_flight: int


def budget_refusal(usage: BudgetUsage) -> Optional[str]:
    """Why the org cannot start another alert RCA now, or None when it can."""
    if usage.in_flight >= MAX_CONCURRENT:
        return REASON_CONCURRENCY
    if usage.started_in_hour >= MAX_PER_HOUR:
        return REASON_HOURLY
    if usage.started_in_window >= MAX_PER_WINDOW:
        return REASON_RATE
    return None


def drain_order(q: QueuedIncident, retrying: bool = False):
    """Sort key for the queue, mirrored by next_queued's ORDER BY.

    Incidents whose last start failed (*retrying*) go after the others, so a
    batch that keeps failing cannot hold the head of the queue pass after pass;
    then most severe first, then oldest.
    """
    return (retrying, _SEVERITY_RANK.get((q.severity or "").lower(), 5), q.queued_at, q.id)


# --- Storage --------------------------------------------------------------------

class SqlStore:
    """The queue's reads and writes, on a connection already in the org's RLS context."""

    def __init__(self, cursor, conn, org_id: str):
        self.cursor = cursor
        self.conn = conn
        self.org_id = org_id

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def lock_org(self) -> None:
        """Serialize budget decisions for the org until the transaction ends."""
        # The webhook's own connection may still hold the incident row; fail
        # rather than wait on it forever.
        self.cursor.execute("SET LOCAL lock_timeout = '10s'")
        self.cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))", (f"alert_rca_budget:{self.org_id}",)
        )

    def lock_incident(self, incident_id: str) -> Optional[str]:
        """The incident's aurora_status ('' when unset), or None when it is not visible."""
        self.cursor.execute(
            "SELECT COALESCE(aurora_status, '') FROM incidents WHERE id = %s FOR UPDATE",
            (incident_id,),
        )
        row = self.cursor.fetchone()
        return row[0] if row else None

    def dispatched_recently(self, incident_id: str) -> bool:
        """An RCA was started for this incident moments ago (a retried delivery)."""
        self.cursor.execute(
            "SELECT 1 FROM incidents WHERE id = %s AND rca_dispatched_at > (NOW() AT TIME ZONE 'UTC') - make_interval(secs => %s)",
            (incident_id, _REDISPATCH_GUARD_SECONDS),
        )
        return self.cursor.fetchone() is not None

    def usage(self) -> BudgetUsage:
        self.cursor.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE rca_dispatched_at > (NOW() AT TIME ZONE 'UTC') - make_interval(secs => %s)),
                COUNT(*) FILTER (WHERE rca_dispatched_at > (NOW() AT TIME ZONE 'UTC') - interval '1 hour'),
                COUNT(*) FILTER (WHERE status != 'merged' AND (
                    (aurora_status IN ('running', 'summarizing')
                     AND updated_at > LOCALTIMESTAMP - make_interval(hours => %s))
                    OR (COALESCE(aurora_status, 'idle') = 'idle'
                        AND rca_dispatched_at > (NOW() AT TIME ZONE 'UTC') - make_interval(mins => %s))
                ))
            FROM incidents
            WHERE org_id = %s
              AND (rca_dispatched_at > (NOW() AT TIME ZONE 'UTC') - make_interval(hours => %s)
                   OR aurora_status IN ('running', 'summarizing'))
            """,
            (WINDOW_SECONDS, _RUNNING_MAX_HOURS, _DISPATCH_PICKUP_MINUTES, self.org_id, _RUNNING_MAX_HOURS),
        )
        row = self.cursor.fetchone() or (0, 0, 0)
        return BudgetUsage(int(row[0] or 0), int(row[1] or 0), int(row[2] or 0))

    def has_queued(self, include_failed: bool = True) -> bool:
        """Whether the org has queued incidents.

        The drain counts all of them. A new alert only waits behind incidents
        that are waiting for budget, not behind ones whose start keeps failing
        (include_failed=False), or a broken one would delay every alert.
        """
        sql = "SELECT 1 FROM incidents WHERE org_id = %s AND aurora_status = %s AND " + _DRAINABLE_SQL
        params: tuple = (self.org_id, QUEUED)
        if not include_failed:
            sql += " AND rca_queue_reason IS DISTINCT FROM %s"
            params += (REASON_DISPATCH_FAILED,)
        self.cursor.execute(sql + " LIMIT 1", params)
        return self.cursor.fetchone() is not None

    def queue(self, incident_id: str, reason: str, request: Optional[RcaRequest]) -> None:
        self.cursor.execute(
            """
            UPDATE incidents
            SET aurora_status = %s, rca_queue_reason = %s,
                rca_queued_at = COALESCE(rca_queued_at, NOW() AT TIME ZONE 'UTC'),
                rca_queued_request = COALESCE(%s::jsonb, rca_queued_request),
                updated_at = LOCALTIMESTAMP
            WHERE id = %s
            """,
            (QUEUED, reason, request.to_json() if request else None, incident_id),
        )

    def refresh_request(self, incident_id: str, request: RcaRequest) -> None:
        """The same alert fired again while waiting: keep its place, use the newest payload."""
        self.cursor.execute(
            "UPDATE incidents SET rca_queued_request = %s::jsonb WHERE id = %s",
            (request.to_json(), incident_id),
        )

    def mark_dispatched(self, incident_id: str, request: Optional[RcaRequest] = None) -> None:
        """Reserve a start for the incident.

        The request stays on the row until the task is published (set_task),
        so a worker that dies between this commit and the publish leaves
        something the stale sweep can put back in the queue. updated_at moves so
        the never-started sweep counts from now, not from when the alert arrived.

        rca_dispatched_at and rca_queued_at hold the UTC instant (naive), the
        contract iso_utc relies on when the API returns them; every comparison
        on them uses the same UTC clock. updated_at stays on the session clock
        the stale sweep compares it with.
        """
        self.cursor.execute(
            """
            UPDATE incidents
            SET aurora_status = 'idle', rca_dispatched_at = (NOW() AT TIME ZONE 'UTC'),
                rca_queue_reason = NULL, rca_queued_at = NULL,
                rca_queued_request = COALESCE(%s::jsonb, rca_queued_request),
                rca_celery_task_id = NULL, updated_at = LOCALTIMESTAMP
            WHERE id = %s
            """,
            (request.to_json() if request else None, incident_id),
        )

    def release_dispatch(self, incident_id: str) -> None:
        """A start that was never published does not count against the budget."""
        self.cursor.execute(
            "UPDATE incidents SET rca_dispatched_at = NULL WHERE id = %s",
            (incident_id,),
        )

    def next_queued(self, skip_ids: List[str]) -> Optional[QueuedIncident]:
        """Lock and load the next incident to start: most severe, then oldest.

        Only that one row is locked and has its request read. *skip_ids* are
        incidents whose start already failed in this pass.
        """
        self.cursor.execute(
            f"""
            SELECT id, user_id, source_type, severity, rca_queued_at, rca_queued_request
            FROM incidents
            WHERE org_id = %s AND aurora_status = %s AND {_DRAINABLE_SQL}
              AND rca_queued_request IS NOT NULL
              AND NOT (id = ANY(%s::uuid[]))
            ORDER BY (COALESCE(rca_queue_reason, '') = '{REASON_DISPATCH_FAILED}'),
                     {_SEVERITY_ORDER_SQL}, rca_queued_at, id
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """,
            (self.org_id, QUEUED, skip_ids),
        )
        row = self.cursor.fetchone()
        if not row:
            return None
        request = RcaRequest.from_db(row[5])
        if request is None:
            return None
        return QueuedIncident(
            id=str(row[0]), user_id=row[1], source_type=row[2] or "", severity=row[3],
            queued_at=row[4], request=request,
        )

    def set_reason_all(self, reason: str) -> None:
        self.cursor.execute(
            # A failed start keeps its reason: it is what orders it after the others.
            "UPDATE incidents SET rca_queue_reason = %s WHERE org_id = %s AND aurora_status = %s"
            " AND rca_queue_reason IS DISTINCT FROM %s AND rca_queue_reason IS DISTINCT FROM %s",
            (reason, self.org_id, QUEUED, reason, REASON_DISPATCH_FAILED),
        )

    def set_task(self, incident_id: str, task_id: str) -> None:
        # Published: the request is no longer needed for recovery.
        self.cursor.execute(
            "UPDATE incidents SET rca_celery_task_id = %s, rca_queued_request = NULL,"
            " rca_start_attempts = 0 WHERE id = %s",
            (task_id, incident_id),
        )

    def record_failed_start(self, incident_id: str) -> int:
        """Count a failed start; returns the failures in a row so far."""
        self.cursor.execute(
            "UPDATE incidents SET rca_start_attempts = COALESCE(rca_start_attempts, 0) + 1"
            " WHERE id = %s RETURNING rca_start_attempts",
            (incident_id,),
        )
        row = self.cursor.fetchone()
        return int(row[0]) if row and row[0] is not None else 1

    def abandon(self, incident_id: str) -> None:
        """Stop retrying an incident that keeps failing to start: it shows as an error."""
        self.cursor.execute(
            """
            UPDATE incidents
            SET aurora_status = 'error', rca_queue_reason = %s,
                status = CASE WHEN status = 'investigating' THEN 'analyzed' ELSE status END,
                analyzed_at = COALESCE(analyzed_at, NOW()), updated_at = LOCALTIMESTAMP
            WHERE id = %s
            """,
            (REASON_DISPATCH_FAILED, incident_id),
        )


# --- Starting an investigation ----------------------------------------------------

def start_rca(user_id: str, incident_id: str, request: RcaRequest, org_id: Optional[str] = None) -> str:
    """Create the RCA's chat session and enqueue it. Returns the Celery task id.

    *org_id* is the org the incident was queued in. The session helper refuses
    an owner who has since moved to another org, so a queued request is never
    started in the wrong org.

    When the broker refuses the task, the session is marked failed before the
    error propagates, so a retry does not leave an extra chat in progress.
    """
    from chat.background.task import (
        _update_session_status, create_background_chat_session, run_background_chat,
    )

    session_id = create_background_chat_session(
        user_id=user_id,
        title=request.session_title,
        trigger_metadata=request.trigger_metadata,
        incident_id=incident_id,
        org_id=org_id,
    )
    try:
        task = run_background_chat.delay(
            user_id=user_id,
            session_id=session_id,
            initial_message=request.prompt,
            trigger_metadata=request.trigger_metadata,
            incident_id=incident_id,
            rail_text=request.rail_text,
        )
    except Exception:
        try:
            _update_session_status(session_id, "failed", user_id=user_id)
        except Exception:
            logger.warning("[RcaDispatch] Could not mark session %s failed", sanitize(session_id))
        raise
    return task.id


def _notify(user_id: str, org_id: str, incident_id: str, source: str) -> None:
    try:
        from routes.incidents_sse import broadcast_incident_update_to_user_connections
        broadcast_incident_update_to_user_connections(
            user_id, {"type": "incident_update", "incident_id": str(incident_id), "source": source},
            org_id=org_id,
        )
    except Exception:
        logger.debug("[RcaDispatch] SSE notify failed for %s", sanitize(incident_id), exc_info=True)


def _start_or_requeue(store, user_id: str, incident_id: str, request: RcaRequest) -> str:
    """Start a reserved RCA. Returns DISPATCHED, QUEUED_OUTCOME, ABANDONED or FAILED.

    On a failed start the reservation is released and the incident goes back
    in the queue: QUEUED_OUTCOME. After MAX_START_ATTEMPTS failures in a row it
    is abandoned instead and shows as an error: ABANDONED. If even that write
    fails, FAILED: the request is still on the row from the reservation, so the
    stale sweep puts it back in the queue later.
    """
    try:
        task_id = start_rca(user_id, incident_id, request, org_id=store.org_id)
    except Exception:
        try:
            store.rollback()
            store.release_dispatch(incident_id)
            attempts = store.record_failed_start(incident_id)
            if attempts >= MAX_START_ATTEMPTS:
                store.abandon(incident_id)
            else:
                store.queue(incident_id, REASON_DISPATCH_FAILED, request)
            store.commit()
        except Exception:
            store.rollback()
            logger.exception("[RcaDispatch] Could not start or requeue incident %s", sanitize(incident_id))
            return FAILED
        if attempts >= MAX_START_ATTEMPTS:
            logger.error("[RcaDispatch] Gave up on incident %s after %d failed starts",
                         sanitize(incident_id), attempts)
            return ABANDONED
        # One line, no traceback: a start that keeps failing is retried every minute.
        logger.warning("[RcaDispatch] Could not start RCA for incident %s (attempt %d of %d); queued for retry",
                       sanitize(incident_id), attempts, MAX_START_ATTEMPTS, exc_info=attempts == 1)
        return QUEUED_OUTCOME
    try:
        store.set_task(incident_id, task_id)
        store.commit()
    except Exception:
        # The worker links its own task id; this one is for early cancellation only.
        store.rollback()
        logger.warning("[RcaDispatch] Could not store task id for incident %s", sanitize(incident_id))
    return DISPATCHED


def dispatch_with_store(store, user_id: str, incident_id: str, request: RcaRequest) -> str:
    """Start *incident_id*'s RCA now if the org has budget, else queue it with a reason."""
    store.lock_org()
    state = store.lock_incident(incident_id)
    if state is None:
        store.rollback()
        return MISSING
    if state == QUEUED:
        store.refresh_request(incident_id, request)
        store.commit()
        return QUEUED_OUTCOME
    if state in _IN_FLIGHT_STATES or store.dispatched_recently(incident_id):
        # Its RCA is already running (or just started): a re-fired alert must
        # not queue it, which would hide the running RCA and allow a second.
        store.commit()
        return DISPATCHED
    # Alerts already waiting go first: a new one takes its turn behind them, so
    # the drain can start the most severe one rather than whichever came last.
    reason = REASON_WAITING if store.has_queued(include_failed=False) else budget_refusal(store.usage())
    if reason:
        store.queue(incident_id, reason, request)
        store.commit()
        return QUEUED_OUTCOME
    store.mark_dispatched(incident_id, request)
    store.commit()
    return _start_or_requeue(store, user_id, incident_id, request)


def dispatch_alert_rca(
    *,
    user_id: str,
    incident_id: Any,
    source: str,
    prompt: str,
    rail_text: Optional[str],
    trigger_metadata: Optional[Dict[str, Any]] = None,
    session_title: Optional[str] = None,
    log_prefix: str = "[RcaDispatch]",
) -> str:
    """The one way an alert webhook starts its RCA.

    Returns ``dispatched`` (started now), ``queued`` (waiting, with a visible
    reason; the drain starts it later), ``missing`` (the incident is not
    visible) or ``failed`` (a temporary error stopped it before anything was
    recorded; a caller that can retry its delivery should). Never raises.
    """
    incident_id = str(incident_id)
    request = RcaRequest(
        source=source,
        prompt=prompt,
        rail_text=rail_text,
        session_title=session_title or "RCA",
        trigger_metadata=dict(trigger_metadata or {"source": source}),
    )
    org_id = None
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                org_id = set_rls_context(cursor, conn, user_id, log_prefix=log_prefix)
                if not org_id:
                    return MISSING
                outcome = dispatch_with_store(SqlStore(cursor, conn, org_id), user_id, incident_id, request)
    except Exception:
        logger.exception("%s Failed to dispatch RCA for incident %s", log_prefix, sanitize(incident_id))
        return FAILED
    if outcome == QUEUED_OUTCOME:
        logger.info("%s RCA for incident %s queued (alert RCA budget)", log_prefix, sanitize(incident_id))
        _notify(user_id, org_id, incident_id, source)
    elif outcome == DISPATCHED:
        logger.info("%s Triggered background RCA for incident %s", log_prefix, sanitize(incident_id))
    return outcome


# --- Draining the queue -------------------------------------------------------------

def drain_with_store(store) -> Dict[str, int]:
    """Start queued RCAs, one per incident, while the org's budget allows.

    The budget is checked before the queue is read, so an org at capacity costs
    one count query a minute. Each start locks and loads only the next incident
    (most severe, then oldest). An incident whose start fails is skipped for
    the rest of the pass, so it cannot hold back the others.
    """
    stats = {"dispatched": 0}
    skipped: List[str] = []
    # The cap counts starts, not attempts: incidents that keep failing are
    # skipped without using it up, so they cannot crowd out a healthy one
    # queued behind them. _MAX_FAILED_PER_PASS bounds the publish attempts.
    while stats["dispatched"] < _MAX_DISPATCHES_PER_PASS and len(skipped) < _MAX_FAILED_PER_PASS:
        store.lock_org()
        if not store.has_queued():
            store.commit()
            break
        reason = budget_refusal(store.usage())
        if reason:
            # Say why they are still waiting; the next pass tries again.
            store.set_reason_all(reason)
            store.commit()
            break
        nxt = store.next_queued(skipped)
        if nxt is None:
            store.commit()
            break
        store.mark_dispatched(nxt.id, nxt.request)
        store.commit()
        if _start_or_requeue(store, nxt.user_id, nxt.id, nxt.request) == DISPATCHED:
            stats["dispatched"] += 1
            logger.info("[RcaDispatch] Started queued RCA for incident %s", sanitize(nxt.id))
            _notify(nxt.user_id, store.org_id, nxt.id, nxt.source_type)
        else:
            skipped.append(nxt.id)
    return stats


def _one_user_per_org(cursor) -> List[str]:
    """users has no RLS; any member's id sets the org's RLS context."""
    cursor.execute(
        "SELECT DISTINCT ON (org_id) id FROM users WHERE org_id IS NOT NULL ORDER BY org_id, id"
    )
    return [r[0] for r in cursor.fetchall()]


@celery_app.task(name="chat.background.drain_queued_rcas")
def drain_queued_rcas() -> Dict[str, int]:
    """Start queued alert RCAs as budget frees up. Runs every minute."""
    totals = {"dispatched": 0}
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                for uid in _one_user_per_org(cursor):
                    try:
                        org_id = set_rls_context(cursor, conn, uid, log_prefix="[RcaDispatch:Drain]")
                        if not org_id:
                            continue
                        stats = drain_with_store(SqlStore(cursor, conn, org_id))
                        totals["dispatched"] += stats["dispatched"]
                    except Exception:
                        conn.rollback()
                        logger.exception("[RcaDispatch:Drain] Drain failed for one org")
    except Exception:
        logger.exception("[RcaDispatch:Drain] Drain failed")
    if totals["dispatched"]:
        logger.info("[RcaDispatch:Drain] Started %d queued RCA(s)", totals["dispatched"])
    return totals


# --- Run by hand --------------------------------------------------------------------

class RunRefused(Exception):
    """The incident cannot be investigated from the queue now; the message says why."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.public_message = message
        self.status = status


def run_queued_investigation(incident_id: str, requested_by: str, org_id: str) -> Dict[str, Any]:
    """Start a queued incident's RCA now, at a person's request.

    The person chose to spend it, so the budget does not hold it back; it still
    counts against the budget for the alerts that follow. Runs as the incident's
    owner, like the alert-driven run would have.
    """
    refusal: Optional[RunRefused] = None
    owner_id = request = None
    outcome: Optional[str] = None
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            row = None
            if set_rls_context(cursor, conn, requested_by, org_id=org_id, log_prefix="[RunQueuedRCA]"):
                cursor.execute(
                    "SELECT user_id, aurora_status, rca_queued_request FROM incidents WHERE id = %s FOR UPDATE",
                    (incident_id,),
                )
                row = cursor.fetchone()
            if not row:
                refusal = RunRefused("Incident not found", 404)
            else:
                owner_id, aurora_status, raw = row
                request = RcaRequest.from_db(raw)
                if aurora_status != QUEUED:
                    refusal = RunRefused("This incident is not waiting for an investigation")
                elif request is None:
                    refusal = RunRefused("This incident has no investigation to run", 422)
            store = SqlStore(cursor, conn, org_id)
            if refusal is None:
                store.mark_dispatched(incident_id, request)
                store.commit()
                outcome = _start_or_requeue(store, owner_id, incident_id, request)
            else:
                conn.rollback()
    if refusal is not None:
        raise refusal
    if outcome == QUEUED_OUTCOME:
        raise RunRefused("Could not start the investigation; it is queued and will be retried", 503)
    if outcome == ABANDONED:
        raise RunRefused("Could not start the investigation after several attempts", 500)
    if outcome != DISPATCHED:
        raise RunRefused("Could not start the investigation; try again shortly", 500)
    logger.info("[RunQueuedRCA] Started incident %s on request", sanitize(incident_id))
    return {"started": True}
