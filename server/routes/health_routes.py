"""
Health check endpoints for Aurora production monitoring.
This module provides comprehensive health checks for all Aurora services.
"""

import logging
import time
import os
import requests
import socket
from datetime import datetime
from flask import Blueprint, jsonify
import psycopg2
import redis
from celery_config import celery_app
import websockets
import asyncio
import json


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
            password=password
        )

        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        cursor.close()
        conn.close()

        return {"status": "healthy", "message": "Database connection successful"}
    except Exception as e:
        logger.warning(f"Database health check failed: {e}")
        return {"status": "unhealthy", "error": str(e)}

def check_redis_health():
    """Check Redis connectivity."""
    if not redis:
        return {"status": "unhealthy", "error": "redis library not installed"}
    try:
        redis_url = os.getenv('REDIS_URL', 'redis://redis:6379/0')
        r = redis.from_url(redis_url)
        r.ping()
        return {"status": "healthy", "message": "Redis connection successful"}
    except Exception as e:
        logger.warning(f"Redis health check failed: {e}")
        return {"status": "unhealthy", "error": str(e)}

def check_weaviate_health():
    """Check Weaviate vector database connectivity."""
    try:
        weaviate_host = os.getenv('WEAVIATE_HOST', 'weaviate')
        weaviate_port = os.getenv('WEAVIATE_PORT', '8080')
        response = requests.get(f"http://{weaviate_host}:{weaviate_port}/v1/.well-known/ready", timeout=5)
        response.raise_for_status()
        return {"status": "healthy", "message": "Weaviate connection successful"}
    except requests.RequestException as e:
        logger.warning(f"Weaviate health check failed: {e}")
        return {"status": "unhealthy", "error": f"HTTP error: {e}"}
    except Exception as e:
        logger.warning(f"Weaviate health check failed: {e}")
        return {"status": "unhealthy", "error": str(e)}

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
        return {"status": "unhealthy", "error": str(e)}

async def send_chatbot_test_message():
    """Send a test message to the chatbot and verify the response."""
    # Use env var for service discovery, with fallback for backwards compatibility
    chatbot_host = os.getenv('CHATBOT_HOST', 'chatbot')
    chatbot_port = os.getenv('CHATBOT_PORT', '5006')
    uri = f"ws://{chatbot_host}:{chatbot_port}"
    
    try:
        async with websockets.connect(uri, ping_interval=None) as websocket:
            # Send init message
            await websocket.send(json.dumps({
                "type": "init",
                "user_id": "user_32760a7xMqdpxteAQgVbt1cKc6b"
            }))

            # Send a test query
            await websocket.send(json.dumps({
                "query": "Hello",
                "user_id": "user_32760a7xMqdpxteAQgVbt1cKc6b",
                "session_id": "health_check_session"
            }))

            # Wait for a response
            response = await asyncio.wait_for(websocket.recv(), timeout=10.0)
            logger.info(f"Chatbot health check response: {response}")
            
            # Simple check for a valid JSON response
            try:
                response_data = json.loads(response)
                if response_data.get("type") == "status" and response_data.get("data", {}).get("status") == "START":
                    return {"status": "healthy", "message": "Chatbot connection and initial response successful"}
            except json.JSONDecodeError:
                return {"status": "unhealthy", "error": "Invalid JSON response from chatbot"}

    except asyncio.TimeoutError:
        return {"status": "unhealthy", "error": "Timeout waiting for chatbot response"}
    except websockets.exceptions.ConnectionClosed as e:
        return {"status": "unhealthy", "error": f"Chatbot connection closed unexpectedly: {e}"}
    except Exception as e:
        return {"status": "unhealthy", "error": f"An error occurred during chatbot health check: {e}"}

def check_chatbot_websocket():
    """Check chatbot WebSocket service by sending a test message."""
    try:
        return asyncio.run(send_chatbot_test_message())
    except Exception as e:
        logger.warning(f"Chatbot health check failed: {e}")
        return {"status": "unhealthy", "error": str(e)}


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
        "timestamp": datetime.utcnow().isoformat(),
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
