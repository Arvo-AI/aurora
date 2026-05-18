"""OpenSearch connector routes — connect, disconnect, status, search."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from flask import Blueprint, jsonify, request

from connectors.opensearch_connector.client import OpenSearchClient, OpenSearchError
from utils.auth.rbac_decorators import require_permission
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.log_sanitizer import sanitize
from utils.secrets.secret_ref_utils import delete_user_secret

logger = logging.getLogger(__name__)

opensearch_bp = Blueprint("opensearch", __name__)


def _get_creds(user_id: str) -> Optional[Dict[str, Any]]:
    try:
        return get_token_data(user_id, "opensearch")
    except Exception:
        logger.exception("[OPENSEARCH] Failed to retrieve credentials for %s", sanitize(user_id))
        return None


def _make_client(creds: Dict[str, Any]) -> OpenSearchClient:
    return OpenSearchClient(
        endpoint=creds["endpoint"],
        username=creds["username"],
        password=creds["password"],
        index_pattern=creds.get("index_pattern", "*"),
        verify_ssl=creds.get("verify_ssl", True),
        max_retries=creds.get("max_retries", 2),
    )


# ---------------------------------------------------------------------------
# Connect
# ---------------------------------------------------------------------------

@opensearch_bp.route("/connect", methods=["POST"])
@require_permission("connectors", "write")
def connect(user_id):
    """Validate and store OpenSearch credentials."""
    data = request.get_json(force=True, silent=True) or {}

    raw_endpoint = data.get("endpoint", "").strip()
    username = data.get("username", "").strip()
    password = data.get("password", "")
    index_pattern = data.get("indexPattern", "*").strip() or "*"
    raw_verify_ssl = data.get("verifySsl", True)
    if isinstance(raw_verify_ssl, bool):
        verify_ssl = raw_verify_ssl
    elif isinstance(raw_verify_ssl, str):
        verify_ssl = raw_verify_ssl.strip().lower() in {"1", "true", "yes", "on"}
    else:
        return jsonify({"error": "verifySsl must be a boolean"}), 400

    try:
        max_retries = int(data.get("maxRetries", 2))
    except (TypeError, ValueError):
        return jsonify({"error": "maxRetries must be an integer"}), 400

    if not raw_endpoint:
        return jsonify({"error": "endpoint is required"}), 400
    if not username or not password:
        return jsonify({"error": "username and password are required"}), 400

    try:
        endpoint = OpenSearchClient.normalize_endpoint(raw_endpoint)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    logger.info(
        "[OPENSEARCH] Connecting user %s to %s",
        sanitize(user_id),
        sanitize(endpoint),
    )

    client = OpenSearchClient(
        endpoint=endpoint,
        username=username,
        password=password,
        index_pattern=index_pattern,
        verify_ssl=verify_ssl,
        max_retries=max_retries,
    )

    try:
        info = client.cluster_info()
        health = client.health()
    except OpenSearchError as exc:
        logger.warning("[OPENSEARCH] Connection failed for %s: %s", sanitize(user_id), exc)
        return jsonify({"error": str(exc)}), 502

    cluster_name = info.get("cluster_name", "opensearch")
    version_info = info.get("version", {})
    version = version_info.get("number", "")
    distribution = version_info.get("distribution", "opensearch")
    cluster_status = health.get("status", "unknown")

    token_payload: Dict[str, Any] = {
        "endpoint": endpoint,
        "username": username,
        "password": password,
        "index_pattern": index_pattern,
        "verify_ssl": verify_ssl,
        "max_retries": max_retries,
        "cluster_name": cluster_name,
        "version": version,
        "distribution": distribution,
    }

    try:
        store_tokens_in_db(user_id, token_payload, "opensearch")
        logger.info("[OPENSEARCH] Stored credentials for %s (cluster=%s)", sanitize(user_id), sanitize(cluster_name))
    except Exception as exc:
        logger.exception("[OPENSEARCH] Failed to store credentials for %s: %s", sanitize(user_id), exc)
        return jsonify({"error": "Failed to store credentials"}), 500

    return jsonify({
        "success": True,
        "clusterName": cluster_name,
        "version": version,
        "distribution": distribution,
        "clusterStatus": cluster_status,
        "endpoint": endpoint,
        "indexPattern": index_pattern,
    })


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

@opensearch_bp.route("/status", methods=["GET"])
@require_permission("connectors", "read")
def status(user_id):
    """Check OpenSearch connection status."""
    creds = _get_creds(user_id)
    if not creds:
        return jsonify({"connected": False})

    if not creds.get("endpoint") or not creds.get("username") or not creds.get("password"):
        return jsonify({"connected": False})

    return jsonify({
        "connected": True,
        "clusterName": creds.get("cluster_name"),
        "version": creds.get("version"),
        "endpoint": creds.get("endpoint"),
        "indexPattern": creds.get("index_pattern", "*"),
    })


# ---------------------------------------------------------------------------
# Disconnect
# ---------------------------------------------------------------------------

@opensearch_bp.route("/disconnect", methods=["POST", "DELETE"])
@require_permission("connectors", "write")
def disconnect(user_id):
    """Remove stored OpenSearch credentials."""
    try:
        success, deleted_count = delete_user_secret(user_id, "opensearch")
        if not success:
            return jsonify({"success": False, "error": "Failed to delete stored credentials"}), 500
        logger.info("[OPENSEARCH] Disconnected for %s (deleted %d entries)", sanitize(user_id), deleted_count)
        return jsonify({"success": True, "deleted": deleted_count})
    except Exception as exc:
        logger.exception("[OPENSEARCH] Disconnect failed for %s: %s", sanitize(user_id), exc)
        return jsonify({"error": "Failed to disconnect OpenSearch"}), 500


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

@opensearch_bp.route("/search", methods=["POST"])
@require_permission("connectors", "read")
def search(user_id):
    """Execute a query against OpenSearch and return matching log entries."""
    creds = _get_creds(user_id)
    if not creds:
        return jsonify({"error": "OpenSearch not connected"}), 400

    data = request.get_json(force=True, silent=True) or {}
    query = data.get("query", "").strip()
    if not query:
        return jsonify({"error": "query is required"}), 400

    index = data.get("index") or creds.get("index_pattern", "*")
    start_time = data.get("startTime")
    end_time = data.get("endTime")
    try:
        size = min(max(1, int(data.get("size", 50))), 200)
    except (TypeError, ValueError):
        return jsonify({"error": "size must be an integer"}), 400
    timestamp_field = data.get("timestampField", "@timestamp")

    client = _make_client(creds)

    try:
        result = client.search(
            query=query,
            index=index,
            start_time=start_time,
            end_time=end_time,
            size=size,
            timestamp_field=timestamp_field,
        )
        return jsonify(result)
    except OpenSearchError as exc:
        logger.warning("[OPENSEARCH] Search failed for %s: %s", sanitize(user_id), exc)
        return jsonify({"error": str(exc)}), 502


# ---------------------------------------------------------------------------
# Indices
# ---------------------------------------------------------------------------

@opensearch_bp.route("/indices", methods=["GET"])
@require_permission("connectors", "read")
def list_indices(user_id):
    """List available indices in the cluster."""
    creds = _get_creds(user_id)
    if not creds:
        return jsonify({"error": "OpenSearch not connected"}), 400

    pattern = request.args.get("pattern") or creds.get("index_pattern", "*")
    client = _make_client(creds)

    try:
        indices = client.list_indices(pattern=pattern)
        return jsonify({"indices": indices})
    except OpenSearchError as exc:
        logger.warning("[OPENSEARCH] List indices failed for %s: %s", sanitize(user_id), exc)
        return jsonify({"error": str(exc)}), 502
