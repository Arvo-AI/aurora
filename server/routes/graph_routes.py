"""
Graph API Routes - /api/graph/* endpoints for the infrastructure dependency graph.
"""

import logging
from flask import Blueprint, request, jsonify
from utils.auth.stateless_auth import get_user_id_from_request
from services.graph.memgraph_client import get_memgraph_client

logger = logging.getLogger(__name__)

graph_bp = Blueprint("graph", __name__, url_prefix="/api/graph")


def _get_user():
    """Extract authenticated user_id from request."""
    user_id = get_user_id_from_request()
    if not user_id:
        return None, jsonify({"error": "Unauthorized"}), 401
    return user_id, None, None


# =========================================================================
# Full Graph
# =========================================================================

@graph_bp.route("", methods=["GET"])
def get_graph():
    """GET /api/graph - Returns the full dependency graph for the authenticated user."""
    user_id, err, code = _get_user()
    if err:
        return err, code
    try:
        client = get_memgraph_client()
        graph = client.export_graph(user_id)
        stats = client.get_graph_stats(user_id)
        graph["stats"] = stats
        return jsonify(graph), 200
    except Exception as e:
        logger.error(f"Error fetching graph for user {user_id}: {e}")
        return jsonify({"error": "Failed to fetch graph"}), 500


# =========================================================================
# Services
# =========================================================================

@graph_bp.route("/services", methods=["GET"])
def list_services():
    """GET /api/graph/services - List all services with optional filters."""
    user_id, err, code = _get_user()
    if err:
        return err, code
    try:
        client = get_memgraph_client()
        resource_type = request.args.get("resource_type")
        provider = request.args.get("provider")
        services = client.list_services(user_id, resource_type=resource_type, provider=provider)
        return jsonify({"services": services, "total": len(services)}), 200
    except Exception as e:
        logger.error(f"Error listing services: {e}")
        return jsonify({"error": "Failed to list services"}), 500


@graph_bp.route("/services/<name>", methods=["GET"])
def get_service(name):
    """GET /api/graph/services/<name> - Get a service with dependencies."""
    user_id, err, code = _get_user()
    if err:
        return err, code
    try:
        client = get_memgraph_client()
        service = client.get_service(user_id, name)
        if not service:
            return jsonify({"error": "Service not found"}), 404
        return jsonify(service), 200
    except Exception as e:
        logger.error(f"Error fetching service {name}: {e}")
        return jsonify({"error": "Failed to fetch service"}), 500


@graph_bp.route("/services/<name>/impact", methods=["GET"])
def get_service_impact(name):
    """GET /api/graph/services/<name>/impact - Get blast radius."""
    user_id, err, code = _get_user()
    if err:
        return err, code
    try:
        client = get_memgraph_client()
        impact = client.get_impact_radius(user_id, name)
        return jsonify(impact), 200
    except Exception as e:
        logger.error(f"Error fetching impact for {name}: {e}")
        return jsonify({"error": "Failed to fetch impact"}), 500


@graph_bp.route("/services", methods=["POST"])
def create_service():
    """POST /api/graph/services - Manually add or update a service."""
    user_id, err, code = _get_user()
    if err:
        return err, code
    try:
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
    except Exception as e:
        logger.error(f"Error creating service: {e}")
        return jsonify({"error": "Failed to create service"}), 500


# =========================================================================
# Dependencies
# =========================================================================

@graph_bp.route("/dependencies", methods=["POST"])
def create_dependency():
    """POST /api/graph/dependencies - Manually add a dependency."""
    user_id, err, code = _get_user()
    if err:
        return err, code
    try:
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
    except Exception as e:
        logger.error(f"Error creating dependency: {e}")
        return jsonify({"error": "Failed to create dependency"}), 500


@graph_bp.route("/dependencies/<dep_id>", methods=["DELETE"])
def delete_dependency(dep_id):
    """DELETE /api/graph/dependencies/<from>::<to> - Remove a dependency."""
    user_id, err, code = _get_user()
    if err:
        return err, code
    try:
        parts = dep_id.split("::")
        if len(parts) != 2:
            return jsonify({"error": "Invalid dependency ID format. Use from_service::to_service"}), 400

        client = get_memgraph_client()
        removed = client.remove_dependency(user_id, parts[0], parts[1])
        if not removed:
            return jsonify({"error": "Dependency not found"}), 404
        return jsonify({"status": "deleted"}), 200
    except Exception as e:
        logger.error(f"Error deleting dependency: {e}")
        return jsonify({"error": "Failed to delete dependency"}), 500


# =========================================================================
# Discovery
# =========================================================================

@graph_bp.route("/discover", methods=["POST"])
def trigger_discovery():
    """POST /api/graph/discover - Trigger an on-demand discovery run."""
    user_id, err, code = _get_user()
    if err:
        return err, code
    try:
        from services.discovery.tasks import run_user_discovery
        task = run_user_discovery.delay(user_id)
        return jsonify({
            "task_id": task.id,
            "status": "started",
            "message": "Discovery scan initiated. Results will be available shortly.",
        }), 202
    except Exception as e:
        logger.error(f"Error triggering discovery: {e}")
        return jsonify({"error": "Failed to trigger discovery"}), 500


# =========================================================================
# Stats
# =========================================================================

@graph_bp.route("/stats", methods=["GET"])
def get_stats():
    """GET /api/graph/stats - Graph statistics."""
    user_id, err, code = _get_user()
    if err:
        return err, code
    try:
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
    except Exception as e:
        logger.error(f"Error fetching stats: {e}")
        return jsonify({"error": "Failed to fetch stats"}), 500
