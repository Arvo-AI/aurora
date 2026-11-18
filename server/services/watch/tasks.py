"""Celery tasks for Aurora Watch: poll each opted-in org's clusters and open incidents.

``watch_tick`` runs every minute from beat. It finds the orgs that turned Aurora
Watch on (org preference ``aurora_watch``) and enqueues one ``watch_cluster``
per connected Kubernetes cluster. ``watch_cluster`` collects the cluster's
state and recent logs, advances the detector, opens or extends incidents for
what it confirmed, and saves the state. A Redis lock per cluster keeps polls
from overlapping when a cluster is slow.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from celery_config import celery_app

logger = logging.getLogger(__name__)

POLL_SECONDS = 60
MAX_BACKOFF_SECONDS = 15 * 60  # an unreadable cluster is retried at least this often
_LOCK_TTL = POLL_SECONDS * 3


def _enabled_orgs() -> list[tuple[str, dict[str, Any]]]:
    """Orgs with Aurora Watch on. Starts from users (not RLS-protected), then reads
    each org's preference under that org's RLS context."""
    from services.watch.store import get_settings
    from utils.db.connection_pool import db_pool

    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT org_id FROM users WHERE org_id IS NOT NULL")
        org_ids = [r[0] for r in cur.fetchall() if r[0]]
    out = []
    for org_id in org_ids:
        try:
            settings = get_settings(org_id)
        except Exception:
            logger.exception("[AuroraWatch] could not read settings for org %s", org_id)
            continue
        if settings["enabled"] and settings.get("owner_user_id"):
            out.append((org_id, settings))
    return out


def _settings_key(settings: dict[str, Any]) -> str:
    """What a failed read depends on: which namespaces are read, as whom, and when the
    settings were last saved (turning Watch off and on again is a fresh start)."""
    return (f"{settings.get('owner_user_id')}|{','.join(settings.get('namespaces') or [])}"
            f"|{settings.get('updated_at')}")


def _owner_in_org(user_id: str, org_id: str) -> bool:
    from utils.db.connection_pool import db_pool

    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM users WHERE id = %s AND org_id = %s", (user_id, org_id))
        return cur.fetchone() is not None


@celery_app.task(name="services.watch.tasks.watch_tick")
def watch_tick() -> dict[str, Any]:
    from services.watch.store import list_clusters
    from utils.flags.feature_flags import is_aurora_watch_enabled

    if not is_aurora_watch_enabled():
        return {"status": "disabled"}
    enqueued = 0
    for org_id, settings in _enabled_orgs():
        owner = settings["owner_user_id"]
        try:
            clusters = list_clusters(owner)
        except Exception:
            logger.exception("[AuroraWatch] could not list clusters for org %s", org_id)
            continue
        for cluster in clusters:
            # A poll still queued when the next tick comes is stale: drop it rather than
            # running a burst of polls seconds apart after a worker backlog.
            watch_cluster.apply_async(args=(org_id, cluster["cluster_id"], cluster.get("name")),
                                      expires=POLL_SECONDS)
            enqueued += 1
    return {"status": "ok", "clusters": enqueued}


@celery_app.task(name="services.watch.tasks.watch_cluster", soft_time_limit=POLL_SECONDS * 2,
                 time_limit=POLL_SECONDS * 2 + 30)
def watch_cluster(org_id: str, cluster_id: str, cluster_name: str | None = None) -> dict[str, Any]:
    from utils.cache.redis_client import get_redis_client

    redis = get_redis_client()
    lock_key = f"aurora_watch:lock:{org_id}:{cluster_id}"
    if redis is not None and not redis.set(lock_key, "1", nx=True, ex=_LOCK_TTL):
        return {"status": "busy"}
    try:
        return poll_cluster(org_id, {"cluster_id": cluster_id, "name": cluster_name or cluster_id})
    finally:
        if redis is not None:
            try:
                redis.delete(lock_key)
            except Exception:
                logger.debug("[AuroraWatch] could not release lock %s", lock_key)


def poll_cluster(org_id: str, cluster: dict[str, Any], runner=None, clock=time.time) -> dict[str, Any]:
    """One poll of one cluster. ``runner`` and ``clock`` are injectable for tests."""
    from services.watch.collector import CommandBlocked, KubectlError, collect_logs, collect_state, internal_api_runner
    from services.watch.detector import current_state, group_findings, step
    from services.watch.snapshot import build_snapshot
    from services.watch.store import get_settings, load_state, save_state

    settings = get_settings(org_id)
    owner = settings.get("owner_user_id")
    if not settings["enabled"] or not owner:
        return {"status": "disabled"}
    cluster_id = cluster["cluster_id"]
    if not _owner_in_org(owner, org_id):
        # No RLS context to record this under: the settings page shows the owner,
        # and turning Watch on again makes the current admin the owner.
        logger.warning("[AuroraWatch] org %s: owner %s left the org; skipping until Watch is turned on again",
                       org_id, owner)
        return {"status": "owner_gone"}

    run = runner or internal_api_runner(owner, org_id, cluster_id)
    state = current_state(load_state(owner, cluster_id))
    # Thresholds are counted in polls: polls must stay a poll interval apart, start to
    # start (a slow read must not make the next on-time tick look early).
    started = clock()
    last = state.get("started_at") or (state.get("prev") or {}).get("taken_at")
    if last and started - last < POLL_SECONDS * 0.8:
        return {"status": "too_soon"}
    # Backing off after failed reads, unless the settings changed since (the error may
    # have said to list namespaces). The same slack as above absorbs tick jitter.
    if state.get("retry_for") != _settings_key(settings):
        for field in ("read_failures", "retry_at", "retry_for"):  # new settings: a fresh start
            state.pop(field, None)
    if started < state.get("retry_at", 0) - POLL_SECONDS * 0.2:
        return {"status": "backing_off"}
    try:
        raw, blind = collect_state(run, settings.get("namespaces"))
    except (CommandBlocked, KubectlError) as exc:
        # Retried less and less often while it keeps failing: a cluster too large to read
        # must not cost its agent a dropped connection every minute.
        failures = state.get("read_failures", 0) + 1
        delay = min(MAX_BACKOFF_SECONDS, POLL_SECONDS * 2 ** (failures - 1))
        state.update(read_failures=failures, retry_at=started + delay, retry_for=_settings_key(settings))
        save_state(owner, cluster_id, state,
                   error=f"cluster not readable: {str(exc)[:200]} (next try in {max(1, round(delay / 60))} min)")
        return {"status": "unreachable"}
    for field in ("read_failures", "retry_at", "retry_for"):
        state.pop(field, None)

    try:
        snapshot = build_snapshot(raw, clock())
        cursors = state.setdefault("log_cursors", {})
        logs, log_blind = collect_logs(run, snapshot, cursors, since_seconds=POLL_SECONDS * 3,
                                       open_subjects=set(state.get("open") or {}), rotation=state.get("polls", 0))
        state, confirmed = step(state, snapshot, logs)
        state["started_at"] = started
        for group in group_findings(confirmed):
            if not group["opens_incident"]:
                logger.info("[AuroraWatch] org=%s cluster=%s notice (%s): %s",
                            org_id, cluster_id, group["severity"], group["title"])
        opened = report_open_findings(owner, org_id, cluster, state)
        save_state(owner, cluster_id, state, blind_spots=blind + log_blind)
    except Exception as exc:
        # Recorded where the Kubernetes page shows it, instead of failing silently every minute.
        logger.exception("[AuroraWatch] poll failed for org %s cluster %s", org_id, cluster_id)
        save_state(owner, cluster_id, None, error=f"poll failed: {type(exc).__name__}")
        return {"status": "error"}
    return {"status": "ok", "polls": state["polls"], "confirmed": len(confirmed), "incidents": opened}


def report_open_findings(owner: str, org_id: str, cluster: dict[str, Any], state: dict[str, Any]) -> list[dict]:
    """Open (or extend) an incident for every incident-worthy open finding that has none yet.

    The incident id is stored on the finding only once the incident exists, so a
    failure here (a DB blip, a timeout) is retried on the next poll instead of
    leaving the outage reported to nobody.
    """
    from services.watch.detector import group_findings
    from services.watch.incidents import open_incident

    unreported = [f for f in (state.get("open") or {}).values()
                  if f["severity"] in ("high", "critical") and not f.get("incident_id")]
    opened = []
    for group in group_findings(unreported):
        try:
            result = open_incident(owner, org_id, cluster, group)
        except Exception:
            logger.exception("[AuroraWatch] could not open incident for org %s cluster %s; will retry",
                             org_id, cluster["cluster_id"])
            continue
        for f in group["findings"]:
            f["incident_id"] = result.get("incident_id")
        opened.append(result)
    return opened
