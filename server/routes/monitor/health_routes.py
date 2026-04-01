"""Monitor health routes -- REST snapshot + SSE stream of system health."""
import logging
import json
import queue
import threading
import time
from datetime import datetime, timezone
from flask import Blueprint, Response
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request

logger = logging.getLogger(__name__)

monitor_health_bp = Blueprint("monitor_health", __name__)

_health_sse_queues_by_org: dict[str, list[queue.Queue]] = {}
_latest_snapshot: dict = {}
_snapshot_lock = threading.Lock()


def _collect_health_snapshot() -> dict:
    """Build a full health payload reusing existing health check functions."""
    from routes.health_routes import (
        check_database_health,
        check_redis_health,
        check_weaviate_health,
        check_celery_health,
    )
    from celery_config import celery_app

    services = {
        "database": check_database_health(),
        "redis": check_redis_health(),
        "weaviate": check_weaviate_health(),
        "celery": check_celery_health(),
    }

    celery_detail = _inspect_celery(celery_app)

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "services": services,
        "celery": celery_detail,
    }


def _inspect_celery(celery_app) -> dict:
    """Gather queue depth, active/reserved/scheduled tasks, and worker list."""
    result: dict = {
        "worker_count": 0,
        "workers": [],
        "active_tasks": 0,
        "reserved_tasks": 0,
        "scheduled_tasks": 0,
    }
    try:
        inspector = celery_app.control.inspect(timeout=3)

        active = inspector.active() or {}
        reserved = inspector.reserved() or {}
        scheduled = inspector.scheduled() or {}

        workers = sorted(active.keys() | reserved.keys() | scheduled.keys())
        result["worker_count"] = len(workers)
        result["workers"] = workers
        result["active_tasks"] = sum(len(v) for v in active.values())
        result["reserved_tasks"] = sum(len(v) for v in reserved.values())
        result["scheduled_tasks"] = sum(len(v) for v in scheduled.values())

        try:
            import redis as _redis, os
            r = _redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"))
            result["queue_depth"] = r.llen("celery")
        except Exception:
            result["queue_depth"] = None
    except Exception:
        logger.exception("Celery inspect failed")

    return result


def _background_health_poller():
    """Background thread that collects health snapshots every 10s and pushes to all SSE queues."""
    global _latest_snapshot
    while True:
        try:
            snapshot = _collect_health_snapshot()
            with _snapshot_lock:
                _latest_snapshot = snapshot
            for scope_queues in list(_health_sse_queues_by_org.values()):
                for q in list(scope_queues):
                    try:
                        q.put_nowait(snapshot)
                    except queue.Full:
                        pass
        except Exception:
            logger.exception("Background health poll failed")
        time.sleep(10)


_poller_started = False
_poller_lock = threading.Lock()


def _ensure_poller():
    global _poller_started
    if _poller_started:
        return
    with _poller_lock:
        if _poller_started:
            return
        t = threading.Thread(target=_background_health_poller, daemon=True)
        t.start()
        _poller_started = True


@monitor_health_bp.route("/api/monitor/health", methods=["GET"])
@require_permission("incidents", "read")
def monitor_health_snapshot(user_id):
    """One-shot JSON health payload."""
    _ensure_poller()
    with _snapshot_lock:
        if _latest_snapshot:
            return Response(json.dumps(_latest_snapshot, default=str), status=200, mimetype="application/json")
    snapshot = _collect_health_snapshot()
    return Response(json.dumps(snapshot, default=str), status=200, mimetype="application/json")


@monitor_health_bp.route("/api/monitor/health/stream", methods=["GET"])
@require_permission("incidents", "read")
def monitor_health_stream(user_id):
    """SSE endpoint that pushes system health every 10 seconds."""
    _ensure_poller()
    org_id = get_org_id_from_request()
    scope_key = org_id or user_id

    def generate():
        msg_queue: queue.Queue = queue.Queue(maxsize=20)

        if scope_key not in _health_sse_queues_by_org:
            _health_sse_queues_by_org[scope_key] = []
        _health_sse_queues_by_org[scope_key].append(msg_queue)

        try:
            with _snapshot_lock:
                if _latest_snapshot:
                    yield f"data: {json.dumps(_latest_snapshot, default=str)}\n\n"

            while True:
                try:
                    snapshot = msg_queue.get(timeout=15)
                    yield f"data: {json.dumps(snapshot, default=str)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            if scope_key in _health_sse_queues_by_org:
                try:
                    _health_sse_queues_by_org[scope_key].remove(msg_queue)
                    if not _health_sse_queues_by_org[scope_key]:
                        del _health_sse_queues_by_org[scope_key]
                except ValueError:
                    pass

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
