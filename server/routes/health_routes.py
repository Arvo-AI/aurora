"""
Health check endpoints for Aurora production monitoring.
This module provides comprehensive health checks for all Aurora services.
"""

import logging
import time
import os
import requests
import socket
from datetime import datetime, timezone
from flask import Blueprint, jsonify
import psycopg2
import redis
from celery_config import celery_app


logger = logging.getLogger(__name__)

# Create blueprint
health_bp = Blueprint('health', __name__)

def check_database_health():
    """Check PostgreSQL database connectivity."""
    try:
        # Connect using unified POSTGRES_* variables
        user = os.getenv('POSTGRES_USER')
        password = os.getenv('POSTGRES_PASSWORD')
        host = os.getenv('POSTGRES_HOST')
        port = os.getenv('POSTGRES_PORT')
        dbname = os.getenv('POSTGRES_DB')

        if not user or not password:
            return {"status": "unhealthy", "error": "Database credentials not configured"}

        conn = psycopg2.connect(
            host=host,
            port=port,
            dbname=dbname,
            user=user,
            password=password,
            sslmode=os.getenv('POSTGRES_SSLMODE', 'prefer') or None,
            sslrootcert=os.getenv('POSTGRES_SSLROOTCERT') or None,
        )

        cursor = conn.cursor()
        # No RLS needed — infrastructure health check
        cursor.execute("SELECT 1")
        cursor.close()
        conn.close()

        return {"status": "healthy", "message": "Database connection successful"}
    except Exception as e:
        logger.warning(f"Database health check failed: {e}", exc_info=True)
        return {"status": "unhealthy", "error": "Database connection failed"}

def check_redis_health():
    """Check Redis connectivity."""
    if not redis:
        return {"status": "unhealthy", "error": "redis library not installed"}
    try:
        from utils.cache.redis_client import get_redis_ssl_kwargs
        redis_url = os.getenv('REDIS_URL', 'redis://redis:6379/0')
        r = redis.from_url(redis_url, **get_redis_ssl_kwargs())
        r.ping()
        return {"status": "healthy", "message": "Redis connection successful"}
    except Exception as e:
        logger.warning(f"Redis health check failed: {e}", exc_info=True)
        return {"status": "unhealthy", "error": "Redis connection failed"}

def check_weaviate_health():
    """Check Weaviate vector database connectivity."""
    try:
        weaviate_host = os.getenv('WEAVIATE_HOST', 'weaviate')
        weaviate_port = os.getenv('WEAVIATE_PORT', '8080')
        weaviate_secure = os.getenv('WEAVIATE_SECURE', 'false').lower() in ('1', 'true', 'yes')
        scheme = "https" if weaviate_secure else "http"
        response = requests.get(f"{scheme}://{weaviate_host}:{weaviate_port}/v1/.well-known/ready", timeout=5)
        response.raise_for_status()
        return {"status": "healthy", "message": "Weaviate connection successful"}
    except requests.RequestException as e:
        logger.warning(f"Weaviate health check failed: {e}")
        return {"status": "unhealthy", "error": "Weaviate HTTP connection failed"}
    except Exception as e:
        logger.warning(f"Weaviate health check failed: {e}")
        return {"status": "unhealthy", "error": "Weaviate health check failed"}

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
        return {"status": "healthy", "message": f"Chatbot accepting connections on {host}:{port}"}
    except (socket.timeout, ConnectionRefusedError, OSError) as e:
        logger.warning(f"Chatbot health check failed at {host}:{port}: {e}")
        return {"status": "unhealthy", "error": f"Chatbot not reachable at {host}:{port}: {e}"}


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
        "weaviate": check_weaviate_health(),
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
    """
    return jsonify({"status": "alive"}), 200

@health_bp.route('/readiness', methods=['GET'])
def readiness_check():
    """
    Kubernetes readiness probe. Checks if critical dependencies are available.
    """
    db_health = check_database_health()
    redis_health = check_redis_health()

    if db_health["status"] == "healthy" and redis_health["status"] == "healthy":
        return jsonify({"status": "ready"}), 200
    else:
        return jsonify({
            "status": "not_ready",
            "checks": {
                "database": db_health,
                "redis": redis_health,
            }
        }), 503
