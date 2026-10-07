"""
Health check endpoints for Aurora production monitoring.
This module provides comprehensive health checks for all Aurora services.
"""

import logging
import time
import os
import socket
from datetime import datetime, timezone
from flask import Blueprint, jsonify
import psycopg2.pool
import redis
from celery_config import celery_app


logger = logging.getLogger(__name__)

# Create blueprint
health_bp = Blueprint('health', __name__)

# The kubelet readiness probe times out at 10s (deploy/helm/aurora/values.yaml,
# server.probes.readiness). The shared pool waits 5s by default before giving up,
# so a probe that used the default wait on a busy pool spent its whole budget
# queueing behind application requests and failed for a reason that is not "the
# database is down". Probes wait 1s for a pooled connection and 2s for Redis.
_PROBE_POOL_WAIT_SECONDS = 1.0
_PROBE_REDIS_TIMEOUT_SECONDS = 2.0


def check_database_health(wait_timeout=None):
    """Check PostgreSQL database connectivity using the shared connection pool.

    ``wait_timeout`` bounds how long to wait for a free pooled connection. When
    every pooled connection is checked out the result is ``degraded`` rather
    than ``unhealthy``: the pool only holds open, working connections, so an
    exhausted pool means the process is busy, not that Postgres is unreachable.
    """
    try:
        user = os.getenv('POSTGRES_USER')
        password = os.getenv('POSTGRES_PASSWORD')

        if not user or not password:
            return {"status": "unhealthy", "error": "Database credentials not configured"}

        from utils.db.connection_pool import db_pool
        with db_pool.get_connection(wait_timeout=wait_timeout) as conn:
            with conn.cursor() as cursor:
                # No RLS needed — infrastructure health check
                cursor.execute("SELECT 1")

        return {"status": "healthy", "message": "Database connection successful"}
    except psycopg2.pool.PoolError as e:
        logger.warning("Database health check: connection pool busy (%s)", e)
        return {"status": "degraded", "warning": "Connection pool busy"}
    except Exception as e:
        logger.warning(f"Database health check failed: {e}", exc_info=True)
        return {"status": "unhealthy", "error": "Database connection failed"}

def check_redis_health(timeout=None):
    """Check Redis connectivity.

    ``timeout`` bounds both connect and command time so a hung Redis cannot
    block the calling thread indefinitely (redis-py has no default timeout).
    """
    if not redis:
        return {"status": "unhealthy", "error": "redis library not installed"}
    r = None
    try:
        from utils.cache.redis_client import get_redis_ssl_kwargs
        redis_url = os.getenv('REDIS_URL', 'redis://redis:6379/0')
        kwargs = dict(get_redis_ssl_kwargs())
        if timeout is not None:
            kwargs.setdefault("socket_connect_timeout", timeout)
            kwargs.setdefault("socket_timeout", timeout)
        r = redis.from_url(redis_url, **kwargs)
        r.ping()
        return {"status": "healthy", "message": "Redis connection successful"}
    except Exception as e:
        logger.warning(f"Redis health check failed: {e}", exc_info=True)
        return {"status": "unhealthy", "error": "Redis connection failed"}
    finally:
        if r is not None:
            try:
                r.close()
            except Exception:
                pass

def check_celery_health():
    """Check Celery worker health."""
    if not celery_app:
        return {"status": "unhealthy", "error": "Celery application not found"}
    try:
        inspect = celery_app.control.inspect()
        active_workers = inspect.active()

        if active_workers:
            return {"status": "healthy", "message": f"{len(active_workers)} Celery workers active"}
        else:
            return {"status": "degraded", "warning": "No active Celery workers found"}
    except Exception as e:
        logger.warning(f"Celery health check failed: {e}")
        return {"status": "unhealthy", "error": "Celery health check failed"}

def check_chatbot_websocket():
    """Check chatbot WebSocket service is accepting connections (TCP only).

    Previous implementation sent a real query that triggered an LLM call,
    blocking a gunicorn thread for 10+ seconds and exhausting the thread pool.
    Now we just verify the port is open.
    """
    internal_url = os.getenv('CHATBOT_INTERNAL_URL')
    if internal_url:
        from urllib.parse import urlparse
        parsed = urlparse(internal_url)
        host = parsed.hostname or 'chatbot'
    else:
        host = os.getenv('CHATBOT_HOST', 'chatbot')
    port = 5006

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(3)
            sock.connect((host, port))
        # Fixed message only — never echo the internal host/port back to callers,
        # since /health is reachable on the external API host without auth.
        return {"status": "healthy", "message": "Chatbot accepting connections"}
    except (socket.timeout, ConnectionRefusedError, OSError) as e:
        # Log the underlying host/port + error server-side for debugging, but return
        # a fixed message so no internal topology or exception detail leaks externally.
        logger.warning(f"Chatbot health check failed at {host}:{port}: {e}")
        return {"status": "unhealthy", "error": "Chatbot unavailable"}


@health_bp.route('/', methods=['GET'])
def health_check():
    """
    Comprehensive health check endpoint for all Aurora services.
    Returns a 503 status code if critical services are unhealthy.
    """
    start_time = time.time()

    checks = {
        "database": check_database_health(),
        "redis": check_redis_health(),
        "celery": check_celery_health(),
        "chatbot_websocket": check_chatbot_websocket(),
    }

    # Determine overall status
    is_unhealthy = any(s["status"] == "unhealthy" for s in checks.values())
    is_degraded = any(s["status"] == "degraded" for s in checks.values())

    if is_unhealthy:
        overall_status = "unhealthy"
        http_status = 503
    elif is_degraded:
        overall_status = "degraded"
        http_status = 200
    else:
        overall_status = "healthy"
        http_status = 200

    response_time = round((time.time() - start_time) * 1000, 2)

    response = {
        "overall_status": overall_status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "response_time_ms": response_time,
        "checks": checks,
    }

    return jsonify(response), http_status

@health_bp.route('/liveness', methods=['GET'])
def liveness_check():
    """
    Kubernetes liveness probe. Checks if the Flask app is running.

    Deliberately touches nothing: it needs one free gunicorn thread and
    nothing else, so it only fails when the process itself is saturated.
    """
    return jsonify({"status": "alive"}), 200

@health_bp.route('/readiness', methods=['GET'])
def readiness_check():
    """
    Kubernetes readiness probe. Checks if critical dependencies are available.

    Waits at most ``_PROBE_POOL_WAIT_SECONDS`` for a pooled DB connection and
    ``_PROBE_REDIS_TIMEOUT_SECONDS`` for Redis so the probe answers well inside
    the kubelet timeout. A busy pool is reported as ``degraded`` with HTTP 200:
    the pod is still serving, it is just under load, and pulling it out of the
    Service (or, one probe later, killing it) would only make the load worse.
    A real Postgres or Redis failure still returns 503.
    """
    db_health = check_database_health(wait_timeout=_PROBE_POOL_WAIT_SECONDS)
    redis_health = check_redis_health(timeout=_PROBE_REDIS_TIMEOUT_SECONDS)

    checks = {"database": db_health, "redis": redis_health}
    db_ok = db_health["status"] in ("healthy", "degraded")
    if db_ok and redis_health["status"] == "healthy":
        status = "ready" if db_health["status"] == "healthy" else "degraded"
        return jsonify({"status": status, "checks": checks}), 200
    else:
        return jsonify({"status": "not_ready", "checks": checks}), 503
