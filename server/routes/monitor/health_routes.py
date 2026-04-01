"""Monitor health routes -- REST snapshot + SSE stream of system health."""
import logging
import json
import time
import queue
import threading
from datetime import datetime, timezone
from flask import Blueprint, Response
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request

logger = logging.getLogger(__name__)

monitor_health_bp = Blueprint("monitor_health", __name__)

_health_sse_queues_by_org: dict[str, list[queue.Queue]] = {}


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


@monitor_health_bp.route("/api/monitor/health", methods=["GET"])
@require_permission("incidents", "read")
def monitor_health_snapshot(user_id):
    """One-shot JSON health payload."""
    snapshot = _collect_health_snapshot()
    return Response(
        json.dumps(snapshot, default=str),
        status=200,
        mimetype="application/json",
    )


@monitor_health_bp.route("/api/monitor/health/stream", methods=["GET"])
@require_permission("incidents", "read")
def monitor_health_stream(user_id):
    """SSE endpoint that pushes system health every 10 seconds."""
    org_id = get_org_id_from_request()
    scope_key = org_id or user_id

    def generate():
        msg_queue: queue.Queue = queue.Queue(maxsize=20)

        if scope_key not in _health_sse_queues_by_org:
            _health_sse_queues_by_org[scope_key] = []
        _health_sse_queues_by_org[scope_key].append(msg_queue)

        try:
            while True:
                try:
                    snapshot = _collect_health_snapshot()
                    yield f"data: {json.dumps(snapshot, default=str)}\n\n"
                except Exception:
                    logger.exception("Error collecting health snapshot for SSE")
                    yield f"data: {json.dumps({'error': 'snapshot_failed'})}\n\n"

                time.sleep(10)
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
