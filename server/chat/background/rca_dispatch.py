"""Start alert-driven RCAs without ever dropping one.

Every webhook that creates an incident hands its RCA to ``dispatch_alert_rca``.
It starts the investigation now when the org has budget for it, and otherwise
queues it on the incident (``aurora_status = 'queued'``) with the reason and the
prompt it would have run. A periodic drain starts queued investigations when
budget frees up, and collapses a burst of queued alerts from one source into a
single investigation: the most severe alert leads, the others are attached to it
as related alerts and marked ``grouped``. Nothing is lost: a queued incident is
shown as "Not investigated yet" with a reason, and can be started by hand.

The budget is per org and covers alert-driven RCAs only. Housekeeping runs
(actions, team routing, prediscovery, chat-triggered RCAs) keep their own
per-user limiter in ``task.is_background_chat_allowed`` and never spend it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
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

# --- Bursts ---------------------------------------------------------------------
# A source's queue is drained once no new alert joined it for SETTLE_SECONDS,
# or once its oldest alert has waited MAX_WAIT_SECONDS, so a burst still
# arriving is not split into many investigations.
SETTLE_SECONDS = 120
MAX_WAIT_SECONDS = 600
# Alerts one grouped investigation covers; the rest wait for the next one.
MAX_GROUP_SIZE = 50
# Related alerts are listed by title only, capped, so a burst costs a bounded
# number of prompt tokens however large it is.
_MAX_LISTED_ALERTS = 25
_MAX_LISTED_TITLE_CHARS = 150
# A delivery retried this soon after its RCA was started does not start another.
_REDISPATCH_GUARD_SECONDS = 120
# Bound on investigations one drain pass starts per org.
_MAX_DISPATCHES_PER_PASS = 10

QUEUED = "queued"
GROUPED = "grouped"
NOT_YET_INVESTIGATED = (QUEUED, GROUPED)

# Why an incident is not investigated yet. Codes are stored in
# incidents.rca_queue_reason and mapped to copy by the client.
REASON_RATE = "rate_limited"
REASON_CONCURRENCY = "concurrency"
REASON_HOURLY = "hourly_limit"
REASON_BURST = "burst"
REASON_DISPATCH_FAILED = "dispatch_failed"
QUEUE_REASONS = frozenset({REASON_RATE, REASON_CONCURRENCY, REASON_HOURLY, REASON_BURST, REASON_DISPATCH_FAILED})

_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

DISPATCHED = "dispatched"
QUEUED_OUTCOME = "queued"
MISSING = "missing"


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
    title: str
    service: Optional[str]
    severity: Optional[str]
    queued_at: datetime
    request: Optional[RcaRequest]


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


def pick_leader(batch: List[QueuedIncident]) -> QueuedIncident:
    """The alert a grouped investigation starts from: most severe, then oldest."""
    return min(
        batch,
        key=lambda q: (_SEVERITY_RANK.get((q.severity or "").lower(), 5), q.queued_at),
    )


def grouped_prompt(leader: RcaRequest, followers: List[QueuedIncident]) -> str:
    """The leader's prompt plus a bounded list of the alerts it also covers."""
    if not followers:
        return leader.prompt
    lines = []
    for q in followers[:_MAX_LISTED_ALERTS]:
        title = " ".join((q.title or "untitled alert").split())[:_MAX_LISTED_TITLE_CHARS]
        lines.append(f"- {title}" + (f" (service: {q.service})" if q.service else ""))
    more = len(followers) - len(lines)
    if more > 0:
        lines.append(f"- ...and {more} more")
    return (
        f"{leader.prompt}\n\n"
        f"## Related alerts in the same burst\n"
        f"{len(followers)} other alert(s) from {leader.source or 'the same source'} arrived "
        "within minutes of this one and are covered by this investigation instead of "
        "each getting their own. Check whether they share this alert's root cause, and "
        "say so in your conclusions:\n" + "\n".join(lines)
    )


def is_settled(items: List[QueuedIncident], now: datetime) -> bool:
    """A source's queue may be drained: the burst stopped, or waited long enough."""
    oldest = min(q.queued_at for q in items)
    newest = max(q.queued_at for q in items)
    return (now - newest) >= timedelta(seconds=SETTLE_SECONDS) or (now - oldest) >= timedelta(seconds=MAX_WAIT_SECONDS)


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

    def now(self) -> datetime:
        # The clock rca_queued_at and rca_dispatched_at are written with.
        self.cursor.execute("SELECT LOCALTIMESTAMP")
        return self.cursor.fetchone()[0]

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
            "SELECT 1 FROM incidents WHERE id = %s AND rca_dispatched_at > LOCALTIMESTAMP - make_interval(secs => %s)",
            (incident_id, _REDISPATCH_GUARD_SECONDS),
        )
        return self.cursor.fetchone() is not None

    def usage(self) -> BudgetUsage:
        self.cursor.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE rca_dispatched_at > LOCALTIMESTAMP - make_interval(secs => %s)),
                COUNT(*) FILTER (WHERE rca_dispatched_at > LOCALTIMESTAMP - interval '1 hour'),
                COUNT(*) FILTER (WHERE status != 'merged' AND (
                    (aurora_status IN ('running', 'summarizing')
                     AND updated_at > LOCALTIMESTAMP - make_interval(hours => %s))
                    OR (COALESCE(aurora_status, 'idle') = 'idle'
                        AND rca_dispatched_at > LOCALTIMESTAMP - make_interval(mins => %s))
                ))
            FROM incidents
            WHERE org_id = %s
              AND (rca_dispatched_at > LOCALTIMESTAMP - make_interval(hours => %s)
                   OR aurora_status IN ('running', 'summarizing'))
            """,
            (WINDOW_SECONDS, _RUNNING_MAX_HOURS, _DISPATCH_PICKUP_MINUTES, self.org_id, _RUNNING_MAX_HOURS),
        )
        row = self.cursor.fetchone() or (0, 0, 0)
        return BudgetUsage(int(row[0] or 0), int(row[1] or 0), int(row[2] or 0))

    def has_queued(self) -> bool:
        self.cursor.execute(
            "SELECT 1 FROM incidents WHERE org_id = %s AND aurora_status = %s AND status = 'investigating' LIMIT 1",
            (self.org_id, QUEUED),
        )
        return self.cursor.fetchone() is not None

    def queue(self, incident_id: str, reason: str, request: Optional[RcaRequest]) -> None:
        self.cursor.execute(
            """
            UPDATE incidents
            SET aurora_status = %s, rca_queue_reason = %s,
                rca_queued_at = COALESCE(rca_queued_at, LOCALTIMESTAMP),
                rca_queued_request = COALESCE(%s::jsonb, rca_queued_request),
                rca_group_leader_id = NULL, updated_at = LOCALTIMESTAMP
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

    def mark_dispatched(self, incident_id: str) -> None:
        # updated_at moves so the never-started sweep counts from now, not from
        # when the alert arrived.
        self.cursor.execute(
            """
            UPDATE incidents
            SET aurora_status = 'idle', rca_dispatched_at = LOCALTIMESTAMP,
                rca_queue_reason = NULL, rca_queued_at = NULL, rca_group_leader_id = NULL,
                rca_queued_request = NULL, updated_at = LOCALTIMESTAMP
            WHERE id = %s
            """,
            (incident_id,),
        )

    def mark_grouped(self, follower_ids: List[str], leader_id: str) -> None:
        # The followers keep their own request, so each can still be run alone.
        self.cursor.execute(
            """
            UPDATE incidents
            SET aurora_status = %s, rca_queue_reason = %s, rca_group_leader_id = %s,
                updated_at = LOCALTIMESTAMP
            WHERE id = ANY(%s::uuid[])
            """,
            (GROUPED, REASON_BURST, leader_id, follower_ids),
        )
        # Shown on the leader as related alerts, like correlated ones.
        self.cursor.execute(
            """
            INSERT INTO incident_alerts
                (user_id, org_id, incident_id, source_type, source_alert_id, alert_title,
                 alert_service, alert_severity, correlation_strategy, correlation_score, alert_metadata)
            SELECT i.user_id, i.org_id, %s, i.source_type, i.source_alert_id, i.alert_title,
                   i.alert_service, i.severity, 'burst', 0, i.alert_metadata
            FROM incidents i
            WHERE i.id = ANY(%s::uuid[])
            """,
            (leader_id, follower_ids),
        )

    def requeue_group(self, leader_id: str) -> None:
        """A grouped start failed: its followers go back to waiting on their own."""
        self.cursor.execute(
            """
            UPDATE incidents
            SET aurora_status = %s, rca_queue_reason = %s, rca_group_leader_id = NULL,
                rca_queued_at = COALESCE(rca_queued_at, LOCALTIMESTAMP), updated_at = LOCALTIMESTAMP
            WHERE rca_group_leader_id = %s AND aurora_status = %s
            """,
            (QUEUED, REASON_DISPATCH_FAILED, leader_id, GROUPED),
        )
        self.cursor.execute(
            "DELETE FROM incident_alerts WHERE incident_id = %s AND correlation_strategy = 'burst'",
            (leader_id,),
        )

    def queued(self) -> List[QueuedIncident]:
        self.cursor.execute(
            """
            SELECT id, user_id, source_type, alert_title, alert_service, severity,
                   rca_queued_at, rca_queued_request
            FROM incidents
            WHERE org_id = %s AND aurora_status = %s AND status = 'investigating'
              AND rca_queued_request IS NOT NULL
            ORDER BY rca_queued_at, id
            FOR UPDATE SKIP LOCKED
            """,
            (self.org_id, QUEUED),
        )
        return [
            QueuedIncident(
                id=str(r[0]), user_id=r[1], source_type=r[2] or "", title=r[3] or "",
                service=r[4], severity=r[5], queued_at=r[6], request=RcaRequest.from_db(r[7]),
            )
            for r in self.cursor.fetchall()
        ]

    def set_reason(self, incident_ids: List[str], reason: str) -> None:
        self.cursor.execute(
            "UPDATE incidents SET rca_queue_reason = %s WHERE id = ANY(%s::uuid[]) AND aurora_status = %s",
            (reason, incident_ids, QUEUED),
        )

    def set_task(self, incident_id: str, task_id: str) -> None:
        self.cursor.execute(
            "UPDATE incidents SET rca_celery_task_id = %s WHERE id = %s",
            (task_id, incident_id),
        )


# --- Starting an investigation ----------------------------------------------------

def start_rca(user_id: str, incident_id: str, request: RcaRequest, prompt: Optional[str] = None) -> str:
    """Create the RCA's chat session and enqueue it. Returns the Celery task id."""
    from chat.background.task import create_background_chat_session, run_background_chat

    session_id = create_background_chat_session(
        user_id=user_id,
        title=request.session_title,
        trigger_metadata=request.trigger_metadata,
        incident_id=incident_id,
    )
    task = run_background_chat.delay(
        user_id=user_id,
        session_id=session_id,
        initial_message=prompt or request.prompt,
        trigger_metadata=request.trigger_metadata,
        incident_id=incident_id,
        rail_text=request.rail_text,
    )
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


def _start_or_requeue(store, user_id: str, incident_id: str, request: RcaRequest,
                      prompt: Optional[str] = None, grouped: bool = False) -> bool:
    """Start a dispatched RCA; on failure put it (and its group) back in the queue."""
    try:
        task_id = start_rca(user_id, incident_id, request, prompt)
    except Exception:
        logger.exception("[RcaDispatch] Could not start RCA for incident %s; queued for retry", sanitize(incident_id))
        try:
            store.rollback()
            store.queue(incident_id, REASON_DISPATCH_FAILED, request)
            if grouped:
                store.requeue_group(incident_id)
            store.commit()
        except Exception:
            store.rollback()
            logger.exception("[RcaDispatch] Could not requeue incident %s", sanitize(incident_id))
        return False
    try:
        store.set_task(incident_id, task_id)
        store.commit()
    except Exception:
        # The worker links its own task id; this one is for early cancellation only.
        store.rollback()
        logger.warning("[RcaDispatch] Could not store task id for incident %s", sanitize(incident_id))
    return True


def dispatch_with_store(store, user_id: str, incident_id: str, request: RcaRequest) -> str:
    """Start *incident_id*'s RCA now if the org has budget, else queue it with a reason."""
    store.lock_org()
    state = store.lock_incident(incident_id)
    if state is None:
        store.rollback()
        return MISSING
    if state in NOT_YET_INVESTIGATED:
        store.refresh_request(incident_id, request)
        store.commit()
        return QUEUED_OUTCOME
    if store.dispatched_recently(incident_id):
        store.commit()
        return DISPATCHED
    # Alerts already waiting go first, and a new one joins them so a burst
    # collapses into one investigation instead of trickling out one by one.
    reason = REASON_BURST if store.has_queued() else budget_refusal(store.usage())
    if reason:
        store.queue(incident_id, reason, request)
        store.commit()
        return QUEUED_OUTCOME
    store.mark_dispatched(incident_id)
    store.commit()
    return DISPATCHED if _start_or_requeue(store, user_id, incident_id, request) else QUEUED_OUTCOME


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
    reason; the drain starts it later) or ``missing``. Never raises: a failure
    leaves the incident queued for retry rather than silently uninvestigated.
    """
    incident_id = str(incident_id)
    request = RcaRequest(
        source=source,
        prompt=prompt,
        rail_text=rail_text,
        session_title=session_title or "RCA",
        trigger_metadata=dict(trigger_metadata or {"source": source}),
    )
    outcome = MISSING
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
        return MISSING
    if outcome == QUEUED_OUTCOME:
        logger.info("%s RCA for incident %s queued (alert RCA budget)", log_prefix, sanitize(incident_id))
        _notify(user_id, org_id, incident_id, source)
    elif outcome == DISPATCHED:
        logger.info("%s Triggered background RCA for incident %s", log_prefix, sanitize(incident_id))
    return outcome


# --- Draining the queue -------------------------------------------------------------

def drain_with_store(store) -> Dict[str, int]:
    """Start the queued investigations the org's budget allows, one per settled source."""
    stats = {"dispatched": 0, "grouped": 0}
    for _ in range(_MAX_DISPATCHES_PER_PASS):
        store.lock_org()
        queued = [q for q in store.queued() if q.request is not None]
        if not queued:
            store.commit()
            break
        now = store.now()
        by_source: Dict[str, List[QueuedIncident]] = {}
        for q in queued:
            by_source.setdefault(q.source_type, []).append(q)
        ready = [items for items in by_source.values() if is_settled(items, now)]
        if not ready:
            store.commit()
            break
        reason = budget_refusal(store.usage())
        if reason:
            # Say why they are still waiting; the next pass tries again.
            store.set_reason([q.id for q in queued], reason)
            store.commit()
            break
        items = min(ready, key=lambda xs: min(q.queued_at for q in xs))
        batch = items[:MAX_GROUP_SIZE]
        leader = pick_leader(batch)
        followers = [q for q in batch if q.id != leader.id]
        store.mark_dispatched(leader.id)
        if followers:
            store.mark_grouped([q.id for q in followers], leader.id)
        store.commit()
        prompt = grouped_prompt(leader.request, followers)
        if _start_or_requeue(store, leader.user_id, leader.id, leader.request, prompt, grouped=bool(followers)):
            stats["dispatched"] += 1
            stats["grouped"] += len(followers)
            logger.info(
                "[RcaDispatch] Started queued RCA for incident %s covering %d grouped alert(s)",
                sanitize(leader.id), len(followers),
            )
            _notify(leader.user_id, store.org_id, leader.id, leader.source_type)
        else:
            break
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
    totals = {"dispatched": 0, "grouped": 0}
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
                        totals["grouped"] += stats["grouped"]
                    except Exception:
                        conn.rollback()
                        logger.exception("[RcaDispatch:Drain] Drain failed for one org")
    except Exception:
        logger.exception("[RcaDispatch:Drain] Drain failed")
    if totals["dispatched"]:
        logger.info("[RcaDispatch:Drain] Started %d queued RCA(s), grouping %d alert(s)",
                    totals["dispatched"], totals["grouped"])
    return totals


# --- Run by hand --------------------------------------------------------------------

class RunRefused(Exception):
    """The incident cannot be investigated from the queue now; the message says why."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.public_message = message
        self.status = status


def run_queued_investigation(incident_id: str, requested_by: str, org_id: str) -> Dict[str, Any]:
    """Start a queued or grouped incident's own RCA now, at a person's request.

    The person chose to spend it, so the budget does not hold it back; it still
    counts against the budget for the alerts that follow. Runs as the incident's
    owner, like the alert-driven run would have.
    """
    refusal: Optional[RunRefused] = None
    owner_id = request = None
    started = False
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
                if aurora_status not in NOT_YET_INVESTIGATED:
                    refusal = RunRefused("This incident is not waiting for an investigation")
                elif request is None:
                    refusal = RunRefused("This incident has no investigation to run", 422)
            store = SqlStore(cursor, conn, org_id)
            if refusal is None:
                store.mark_dispatched(incident_id)
                store.commit()
                started = _start_or_requeue(store, owner_id, incident_id, request)
            else:
                conn.rollback()
    if refusal is not None:
        raise refusal
    if not started:
        raise RunRefused("Could not start the investigation; it is queued and will be retried", 503)
    logger.info("[RunQueuedRCA] Started incident %s on request", sanitize(incident_id))
    return {"started": True}
