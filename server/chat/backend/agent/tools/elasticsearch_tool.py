"""Elasticsearch search and query tools for RCA agent."""

import json
import logging
from typing import Any, Dict, Optional

import requests
from pydantic import BaseModel, Field

from utils.auth.token_management import get_token_data

logger = logging.getLogger(__name__)

ELASTICSEARCH_TIMEOUT = 30
ELASTICSEARCH_SEARCH_TIMEOUT = 120
MAX_OUTPUT_SIZE = 2 * 1024 * 1024
MAX_RESULT_SIZE = 10000
MAX_FIELD_VALUE_LENGTH = 1000
MAX_SEARCH_RESULTS_CAP = 1000
MAX_INDICES_RETURN = 500


def _truncate_results(results: list, max_output_size: int = MAX_OUTPUT_SIZE) -> tuple[list, bool]:
    """Truncate results to fit within output size limit."""
    truncated = []
    total_size = 0
    was_truncated = False

    for result in results:
        result_str = json.dumps(result)
        if len(result_str) > MAX_RESULT_SIZE:
            if isinstance(result, dict):
                truncated_result = {}
                for key, value in result.items():
                    if isinstance(value, str) and len(value) > MAX_FIELD_VALUE_LENGTH:
                        truncated_result[key] = value[:MAX_FIELD_VALUE_LENGTH] + "...[truncated]"
                    else:
                        truncated_result[key] = value
                truncated_result['_truncated'] = True
                result = truncated_result
            result_str = json.dumps(result)

        if total_size + len(result_str) > max_output_size:
            was_truncated = True
            break

        truncated.append(result)
        total_size += len(result_str)

    return truncated, was_truncated


class ElasticsearchSearchArgs(BaseModel):
    """Arguments for search_elasticsearch tool."""
    query: str = Field(
        description=(
            "Elasticsearch query string or JSON query DSL. "
            "For simple searches use Lucene syntax: 'error AND service:api-gateway'. "
            "For complex queries pass JSON DSL: '{\"bool\": {\"must\": [{\"match\": {\"message\": \"error\"}}]}}'"
        )
    )
    index: str = Field(default="*", description="Index pattern to search (e.g., 'logs-*', 'filebeat-*', '*')")
    earliest_time: str = Field(default="now-1h", description="Start time (e.g., 'now-1h', 'now-24h', 'now-7d')")
    latest_time: str = Field(default="now", description="End time (default: 'now')")
    size: int = Field(default=100, description="Maximum results to return (default: 100)")
    time_field: str = Field(default="@timestamp", description="Timestamp field name (default: '@timestamp')")


class ElasticsearchListIndicesArgs(BaseModel):
    """Arguments for list_elasticsearch_indices tool."""
    pass


class ElasticsearchClusterHealthArgs(BaseModel):
    """Arguments for elasticsearch_cluster_health tool."""
    pass


def _get_elasticsearch_credentials(user_id: str) -> Optional[Dict[str, Any]]:
    """Get Elasticsearch credentials for a user."""
    try:
        creds = get_token_data(user_id, "elasticsearch")
        if not creds:
            return None
        base_url = creds.get("base_url") or creds.get("host")
        if not base_url:
            return None
        creds["base_url"] = base_url
        return creds
    except Exception as exc:
        logger.error(f"[ES-TOOL] Failed to get credentials: {exc}")
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


def _elasticsearch_auth(creds: Dict[str, Any]) -> Optional[tuple[str, str]]:
    """Return basic auth tuple if using basic auth."""
    if creds.get("auth_method") == "basic" and creds.get("username") and creds.get("password"):
        return (creds["username"], creds["password"])
    return None


def is_elasticsearch_connected(user_id: str) -> bool:
    """Check if Elasticsearch is connected for a user."""
    return _get_elasticsearch_credentials(user_id) is not None


def search_elasticsearch(
    query: str,
    index: str = "*",
    earliest_time: str = "now-1h",
    latest_time: str = "now",
    size: int = 100,
    time_field: str = "@timestamp",
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    """Execute a query against Elasticsearch and return results.

    Supports both Lucene query strings and full JSON query DSL.
    """
    if not user_id:
        return json.dumps({"error": "User context not available"})

    creds = _get_elasticsearch_credentials(user_id)
    if not creds:
        return json.dumps({"error": "Elasticsearch not connected. Please connect Elasticsearch first."})

    size = min(size, MAX_SEARCH_RESULTS_CAP)

    # Build search body
    search_body: Dict[str, Any] = {"size": size}

    # Try parsing query as JSON DSL first
    parsed_query = None
    if query.strip().startswith("{"):
        try:
            parsed_query = json.loads(query)
        except json.JSONDecodeError:
            pass

    if parsed_query:
        time_filter = {"range": {time_field: {"gte": earliest_time, "lte": latest_time}}}
        search_body["query"] = {
            "bool": {
                "must": [parsed_query],
                "filter": [time_filter],
            }
        }
    else:
        search_body["query"] = {
            "bool": {
                "must": [{"query_string": {"query": query}}],
                "filter": [{"range": {time_field: {"gte": earliest_time, "lte": latest_time}}}],
            }
        }

    search_body["sort"] = [{time_field: {"order": "desc", "unmapped_type": "date"}}]

    logger.info(f"[ES-TOOL] Executing search for user {user_id}: index={index}, query={query[:100]}...")

    try:
        url = f"{creds['base_url']}/{index}/_search"

        response = requests.post(
            url,
            headers=_elasticsearch_headers(creds),
            auth=_elasticsearch_auth(creds),
            json=search_body,
            timeout=ELASTICSEARCH_SEARCH_TIMEOUT,
            verify=False,
        )

        if response.status_code == 401:
            return json.dumps({"error": "Elasticsearch authentication failed. Check credentials."})
        elif response.status_code == 400:
            error_msg = response.text[:500] if response.text else "Bad request"
            return json.dumps({"error": f"Invalid query: {error_msg}"})
        elif response.status_code == 404:
            return json.dumps({"error": f"Index pattern '{index}' not found"})

        response.raise_for_status()
        result = response.json()

        hits = result.get("hits", {})
        total = hits.get("total", {})
        total_value = total.get("value", 0) if isinstance(total, dict) else total

        results = [hit.get("_source", {}) for hit in hits.get("hits", [])]
        results, was_truncated = _truncate_results(results)

        output = {
            "total": total_value,
            "returned": len(results),
            "took_ms": result.get("took", 0),
            "timed_out": result.get("timed_out", False),
            "results": results,
        }
        if was_truncated:
            output["warning"] = f"Results truncated from {total_value} to {len(results)} due to size limits"

        return json.dumps(output, default=str)

    except requests.exceptions.Timeout:
        return json.dumps({"error": "Search timed out. Try a narrower time range or simpler query."})
    except requests.exceptions.RequestException as exc:
        logger.error(f"[ES-TOOL] Search failed for user {user_id}: {exc}")
        return json.dumps({"error": f"Search request failed: {str(exc)}"})


def list_elasticsearch_indices(
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    """List available Elasticsearch indices to discover what data is available."""
    if not user_id:
        return json.dumps({"error": "User context not available"})

    creds = _get_elasticsearch_credentials(user_id)
    if not creds:
        return json.dumps({"error": "Elasticsearch not connected. Please connect Elasticsearch first."})

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
            return json.dumps({"error": "Authentication failed"})

        response.raise_for_status()
        indices = response.json()

        visible = [idx for idx in indices if not idx.get("index", "").startswith(".")]
        visible = visible[:MAX_INDICES_RETURN]

        return json.dumps({
            "total": len(visible),
            "indices": visible,
        }, default=str)

    except requests.exceptions.RequestException as exc:
        logger.error(f"[ES-TOOL] Failed to list indices for user {user_id}: {exc}")
        return json.dumps({"error": f"Failed to list indices: {str(exc)}"})


def elasticsearch_cluster_health(
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    """Get Elasticsearch cluster health status including node count, shard status, and overall health."""
    if not user_id:
        return json.dumps({"error": "User context not available"})

    creds = _get_elasticsearch_credentials(user_id)
    if not creds:
        return json.dumps({"error": "Elasticsearch not connected. Please connect Elasticsearch first."})

    try:
        url = f"{creds['base_url']}/_cluster/health"

        response = requests.get(
            url,
            headers=_elasticsearch_headers(creds),
            auth=_elasticsearch_auth(creds),
            timeout=ELASTICSEARCH_TIMEOUT,
            verify=False,
        )

        if response.status_code == 401:
            return json.dumps({"error": "Authentication failed"})

        response.raise_for_status()
        return json.dumps(response.json(), default=str)

    except requests.exceptions.RequestException as exc:
        logger.error(f"[ES-TOOL] Failed to get cluster health for user {user_id}: {exc}")
        return json.dumps({"error": f"Failed to get cluster health: {str(exc)}"})
