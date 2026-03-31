"""Elasticsearch search routes for running queries."""

import json
import logging
from typing import Any, Dict, Optional, Tuple

import requests
from flask import Blueprint, jsonify, request

from utils.web.cors_utils import create_cors_response
from utils.auth.stateless_auth import get_user_id_from_request
from utils.auth.token_management import get_token_data

ELASTICSEARCH_TIMEOUT = 30
ELASTICSEARCH_SEARCH_TIMEOUT = 120

logger = logging.getLogger(__name__)

search_bp = Blueprint("elasticsearch_search", __name__)


def _get_elasticsearch_client_for_user(user_id: str) -> Optional[Dict[str, Any]]:
    """Get Elasticsearch credentials for the user."""
    try:
        creds = get_token_data(user_id, "elasticsearch")
        if not creds:
            return None
        base_url = creds.get("base_url")
        if not base_url:
            return None
        return creds
    except Exception as exc:
        logger.error(f"[ELASTICSEARCH-SEARCH] Failed to get credentials for user {user_id}: {exc}")
        return None


def _elasticsearch_headers(creds: Dict[str, Any]) -> Dict[str, str]:
    """Return headers for Elasticsearch API requests."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if creds.get("api_key"):
        headers["Authorization"] = f"ApiKey {creds['api_key']}"
    return headers


def _elasticsearch_auth(creds: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """Return basic auth tuple if using basic auth."""
    if creds.get("auth_method") == "basic" and creds.get("username") and creds.get("password"):
        return (creds["username"], creds["password"])
    return None


@search_bp.route("/search", methods=["POST", "OPTIONS"])
def search():
    """Execute a search query against Elasticsearch."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    creds = _get_elasticsearch_client_for_user(user_id)
    if not creds:
        return jsonify({"error": "Elasticsearch not connected"}), 400

    data = request.get_json(silent=True) or {}
    index_pattern = data.get("index", "*")
    query_body = data.get("query")
    query_string = data.get("queryString")
    from_param = data.get("from", 0)
    size = data.get("size", 100)
    sort = data.get("sort")
    time_field = data.get("timeField", "@timestamp")
    earliest_time = data.get("earliestTime")
    latest_time = data.get("latestTime")

    # Build the search body
    search_body: Dict[str, Any] = {"from": from_param, "size": size}

    if query_body:
        # Raw query DSL passed directly
        search_body["query"] = query_body
    elif query_string:
        # Simple query string
        must_clauses = [{"query_string": {"query": query_string}}]

        # Add time range filter if provided
        if earliest_time or latest_time:
            range_filter: Dict[str, Any] = {}
            if earliest_time:
                range_filter["gte"] = earliest_time
            if latest_time:
                range_filter["lte"] = latest_time
            must_clauses.append({"range": {time_field: range_filter}})

        search_body["query"] = {"bool": {"must": must_clauses}}
    else:
        # Default: match all with optional time range
        if earliest_time or latest_time:
            range_filter = {}
            if earliest_time:
                range_filter["gte"] = earliest_time
            if latest_time:
                range_filter["lte"] = latest_time
            search_body["query"] = {"range": {time_field: range_filter}}
        else:
            search_body["query"] = {"match_all": {}}

    if sort:
        search_body["sort"] = sort
    else:
        search_body["sort"] = [{time_field: {"order": "desc", "unmapped_type": "date"}}]

    logger.info(f"[ELASTICSEARCH-SEARCH] User {user_id} searching index={index_pattern}")

    try:
        url = f"{creds['base_url']}/{index_pattern}/_search"

        response = requests.post(
            url,
            headers=_elasticsearch_headers(creds),
            auth=_elasticsearch_auth(creds),
            json=search_body,
            timeout=ELASTICSEARCH_SEARCH_TIMEOUT,
            verify=False,
        )

        if response.status_code == 401:
            return jsonify({"error": "Elasticsearch authentication failed. Check your credentials."}), 401
        elif response.status_code == 400:
            error_msg = response.text[:500] if response.text else "Bad request"
            return jsonify({"error": f"Invalid query: {error_msg}"}), 400
        elif response.status_code == 404:
            return jsonify({"error": f"Index pattern '{index_pattern}' not found"}), 404

        response.raise_for_status()
        result = response.json()

        hits = result.get("hits", {})
        total = hits.get("total", {})
        total_value = total.get("value", 0) if isinstance(total, dict) else total

        return jsonify({
            "success": True,
            "results": [hit.get("_source", {}) for hit in hits.get("hits", [])],
            "total": total_value,
            "took": result.get("took", 0),
            "timedOut": result.get("timed_out", False),
        })

    except requests.exceptions.Timeout:
        logger.error(f"[ELASTICSEARCH-SEARCH] Search timeout for user {user_id}")
        return jsonify({"error": "Search timed out. Try a narrower time range or simpler query."}), 504
    except requests.exceptions.RequestException as exc:
        logger.error(f"[ELASTICSEARCH-SEARCH] Search failed for user {user_id}: {exc}", exc_info=True)
        return jsonify({"error": "Search request failed"}), 502


@search_bp.route("/indices", methods=["GET", "OPTIONS"])
def list_indices():
    """List available indices in the Elasticsearch cluster."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    creds = _get_elasticsearch_client_for_user(user_id)
    if not creds:
        return jsonify({"error": "Elasticsearch not connected"}), 400

    try:
        url = f"{creds['base_url']}/_cat/indices?format=json&h=index,health,status,docs.count,store.size"

        response = requests.get(
            url,
            headers=_elasticsearch_headers(creds),
            auth=_elasticsearch_auth(creds),
            timeout=ELASTICSEARCH_TIMEOUT,
            verify=False,
        )

        if response.status_code == 401:
            return jsonify({"error": "Authentication failed"}), 401

        response.raise_for_status()
        indices = response.json()

        # Filter out system indices (starting with .)
        visible_indices = [idx for idx in indices if not idx.get("index", "").startswith(".")]

        return jsonify({
            "success": True,
            "indices": visible_indices,
        })

    except requests.exceptions.RequestException as exc:
        logger.error(f"[ELASTICSEARCH-SEARCH] Failed to list indices for user {user_id}: {exc}", exc_info=True)
        return jsonify({"error": "Failed to list indices"}), 502


@search_bp.route("/cluster/health", methods=["GET", "OPTIONS"])
def cluster_health():
    """Get Elasticsearch cluster health."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    creds = _get_elasticsearch_client_for_user(user_id)
    if not creds:
        return jsonify({"error": "Elasticsearch not connected"}), 400

    try:
        url = f"{creds['base_url']}/_cluster/health"

        response = requests.get(
            url,
            headers=_elasticsearch_headers(creds),
            auth=_elasticsearch_auth(creds),
            timeout=ELASTICSEARCH_TIMEOUT,
            verify=False,
        )

        response.raise_for_status()
        return jsonify(response.json())

    except requests.exceptions.RequestException as exc:
        logger.error(f"[ELASTICSEARCH-SEARCH] Failed to get cluster health for user {user_id}: {exc}", exc_info=True)
        return jsonify({"error": "Failed to get cluster health"}), 502
