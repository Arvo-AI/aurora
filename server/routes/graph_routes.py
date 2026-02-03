"""
Graph API Routes - /api/graph/* endpoints for the infrastructure dependency graph.
"""

import logging
from functools import wraps
from flask import Blueprint, request, jsonify
from utils.auth.stateless_auth import get_user_id_from_request
from services.graph.memgraph_client import get_memgraph_client

logger = logging.getLogger(__name__)

graph_bp = Blueprint("graph", __name__, url_prefix="/api/graph")


def require_auth(error_message):
    """Decorator that handles authentication and top-level error handling.

    Injects `user_id` as the first argument to the wrapped function.
    Any unhandled exception is logged and returned as a 500 JSON response.
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user_id = get_user_id_from_request()
            if not user_id:
                return jsonify({"error": "Unauthorized"}), 401
            try:
                return fn(user_id, *args, **kwargs)
            except Exception as e:
                logger.error(f"{error_message}: {e}")
                return jsonify({"error": error_message}), 500
        return wrapper
    return decorator


# =========================================================================
# Full Graph
# =========================================================================

@graph_bp.route("", methods=["GET"])
@require_auth("Failed to fetch graph")
def get_graph(user_id):
    """GET /api/graph - Returns the full dependency graph for the authenticated user."""
    client = get_memgraph_client()
    graph = client.export_graph(user_id)
    graph["stats"] = client.get_graph_stats(user_id)
    return jsonify(graph), 200


# =========================================================================
# Services
# =========================================================================

@graph_bp.route("/services", methods=["GET"])
@require_auth("Failed to list services")
def list_services(user_id):
    """GET /api/graph/services - List all services with optional filters."""
    client = get_memgraph_client()
    resource_type = request.args.get("resource_type")
    provider = request.args.get("provider")
    services = client.list_services(user_id, resource_type=resource_type, provider=provider)
    return jsonify({"services": services, "total": len(services)}), 200


@graph_bp.route("/services/<name>", methods=["GET"])
@require_auth("Failed to fetch service")
def get_service(user_id, name):
    """GET /api/graph/services/<name> - Get a service with dependencies."""
    client = get_memgraph_client()
    service = client.get_service(user_id, name)
    if not service:
        return jsonify({"error": "Service not found"}), 404
    return jsonify(service), 200


@graph_bp.route("/services/<name>/impact", methods=["GET"])
@require_auth("Failed to fetch impact")
def get_service_impact(user_id, name):
    """GET /api/graph/services/<name>/impact - Get blast radius."""
    client = get_memgraph_client()
    impact = client.get_impact_radius(user_id, name)
    return jsonify(impact), 200


@graph_bp.route("/services", methods=["POST"])
@require_auth("Failed to create service")
def create_service(user_id):
    """POST /api/graph/services - Manually add or update a service."""
    data = request.get_json()
    if not data or not data.get("name"):
        return jsonify({"error": "name is required"}), 400

    client = get_memgraph_client()
    result = client.upsert_service(
        user_id=user_id,
        name=data["name"],
        resource_type=data.get("resource_type", "external"),
        provider=data.get("provider", "external"),
        display_name=data.get("display_name", data["name"]),
        sub_type=data.get("sub_type", ""),
        criticality=data.get("criticality", "medium"),
        endpoint=data.get("endpoint", ""),
        region=data.get("region", ""),
        cloud_resource_id=data.get("cloud_resource_id", ""),
        vpc_id=data.get("vpc_id", ""),
        metadata=data.get("metadata", {}),
    )
    return jsonify(result), 201


# =========================================================================
# Dependencies
# =========================================================================

@graph_bp.route("/dependencies", methods=["POST"])
@require_auth("Failed to create dependency")
def create_dependency(user_id):
    """POST /api/graph/dependencies - Manually add a dependency."""
    data = request.get_json()
    if not data or not data.get("from_service") or not data.get("to_service"):
        return jsonify({"error": "from_service and to_service are required"}), 400

    client = get_memgraph_client()
    result = client.upsert_dependency(
        user_id=user_id,
        from_service=data["from_service"],
        to_service=data["to_service"],
        dep_type=data.get("dependency_type", "http"),
        confidence=1.0,  # Manual edges are always confidence 1.0
        discovered_from=["manual"],
    )
    if not result:
        return jsonify({"error": "One or both services not found"}), 404
    return jsonify(result), 201


@graph_bp.route("/dependencies/<dep_id>", methods=["DELETE"])
@require_auth("Failed to delete dependency")
def delete_dependency(user_id, dep_id):
    """DELETE /api/graph/dependencies/<from>::<to> - Remove a dependency."""
    parts = dep_id.split("::")
    if len(parts) != 2:
        return jsonify({"error": "Invalid dependency ID format. Use from_service::to_service"}), 400

    client = get_memgraph_client()
    removed = client.remove_dependency(user_id, parts[0], parts[1])
    if not removed:
        return jsonify({"error": "Dependency not found"}), 404
    return jsonify({"status": "deleted"}), 200


# =========================================================================
# Discovery
# =========================================================================

@graph_bp.route("/discover", methods=["POST"])
@require_auth("Failed to trigger discovery")
def trigger_discovery(user_id):
    """POST /api/graph/discover - Trigger an on-demand discovery run."""
    from services.discovery.tasks import run_user_discovery
    from utils.cache.redis_client import get_redis_client

    redis_client = get_redis_client()

    # Rate limit: 1 request per 30 seconds per user
    rate_key = f"discovery:rate:{user_id}"
    if redis_client and redis_client.get(rate_key):
        return jsonify({
            "error": "Rate limited. Please wait 30 seconds between discovery requests.",
        }), 429
    if redis_client:
        redis_client.setex(rate_key, 30, "1")

    # Deduplicate: if a discovery task is already running for this user, return its ID
    lock_key = f"discovery:running:{user_id}"
    if redis_client:
        existing_task_id = redis_client.get(lock_key)
        if existing_task_id:
            # Verify the task is still actually running
            existing = run_user_discovery.AsyncResult(existing_task_id)
            if existing.state in ("PENDING", "STARTED", "PROGRESS"):
                return jsonify({
                    "task_id": existing_task_id,
                    "status": "already_running",
                    "message": "Discovery is already in progress.",
                }), 200

    task = run_user_discovery.delay(user_id)

    # Atomic lock: SET NX EX to prevent TOCTOU race between check and set
    if redis_client:
        acquired = redis_client.set(lock_key, task.id, nx=True, ex=10800)
        if not acquired:
            # Another request won the race -- but our task is already dispatched.
            # Overwrite with our task ID since the old lock was stale (we passed
            # the check above, meaning the old task was not running).
            redis_client.setex(lock_key, 10800, task.id)

    return jsonify({
        "task_id": task.id,
        "status": "started",
        "message": "Discovery scan initiated. Results will be available shortly.",
    }), 202


# =========================================================================
# Discovery Status
# =========================================================================

@graph_bp.route("/discover/status/<task_id>", methods=["GET"])
@require_auth("Failed to fetch task status")
def get_discovery_status(user_id, task_id):
    """GET /api/graph/discover/status/<task_id> - Poll Celery task progress."""
    from services.discovery.tasks import run_user_discovery
    from utils.cache.redis_client import get_redis_client

    task = run_user_discovery.AsyncResult(task_id)
    state = task.state

    if state == "PENDING":
        # Celery returns PENDING for unknown/expired task IDs.
        # Check Redis to see if this task was ever dispatched for this user.
        redis_client = get_redis_client()
        lock_key = f"discovery:running:{user_id}"
        active_task = redis_client.get(lock_key) if redis_client else None
        if active_task != task_id:
            response = {"state": "GONE", "status": "Task expired or completed", "complete": True}
        else:
            response = {"state": state, "status": "Starting discovery", "complete": False}
    elif state == "PROGRESS":
        meta = task.info or {}
        response = {"state": state, "status": meta.get("status", "Discovery in progress"), "complete": False}
    elif state == "SUCCESS":
        response = {"state": state, "status": "Discovery completed", "complete": True, "result": task.result or {}}
    elif state == "FAILURE":
        response = {"state": state, "status": str(task.info), "complete": True, "error": True}
    else:
        response = {"state": state, "status": "Discovery is running", "complete": False}

    return jsonify(response)


# =========================================================================
# Stats
# =========================================================================

@graph_bp.route("/stats", methods=["GET"])
@require_auth("Failed to fetch stats")
def get_stats(user_id):
    """GET /api/graph/stats - Graph statistics."""
    client = get_memgraph_client()
    stats = client.get_graph_stats(user_id)

    # Add critical services and SPOFs
    try:
        stats["critical_services"] = [s["service"] for s in client.get_critical_services(user_id)[:5]]
    except Exception:
        stats["critical_services"] = []

    try:
        stats["single_points_of_failure"] = [s["service"] for s in client.get_single_points_of_failure(user_id)]
    except Exception:
        stats["single_points_of_failure"] = []

    return jsonify(stats), 200
