"""Splunk SPL search tool for RCA agent."""

import json
import logging
import re
from typing import Any, Dict, Optional

import requests
from pydantic import BaseModel, Field

from utils.auth.token_management import get_token_data

logger = logging.getLogger(__name__)

SPLUNK_SEARCH_TIMEOUT = 60
MAX_OUTPUT_SIZE = 2 * 1024 * 1024  # 2MB max output
MAX_RESULT_SIZE = 10000  # 10KB max per individual result
MAX_FIELD_VALUE_LENGTH = 1000  # Truncate individual field values longer than this
MAX_INDEXES_RETURN = 500  # Max indexes to return from list_splunk_indexes
MAX_SOURCETYPES_RETURN = 200  # Max sourcetypes to return from list_splunk_sourcetypes
MAX_SEARCH_RESULTS_CAP = 1000  # Hard cap on search results for tool usage

# Pattern for valid Splunk index/sourcetype names: alphanumeric, underscore, hyphen
SAFE_IDENTIFIER_PATTERN = re.compile(r'^[a-zA-Z0-9_\-]+$')


def _validate_splunk_identifier(value: str, field_name: str = "identifier") -> tuple[bool, str]:
    """Validate a Splunk identifier (index, sourcetype) to prevent SPL injection.

    Returns:
        Tuple of (is_valid, error_message)
    """
    if not value:
        return True, ""
    if len(value) > 256:
        return False, f"{field_name} exceeds maximum length (256)"
    if not SAFE_IDENTIFIER_PATTERN.match(value):
        return False, f"{field_name} contains invalid characters (only alphanumeric, underscore, hyphen allowed)"
    return True, ""


def _truncate_results(results: list, max_output_size: int = MAX_OUTPUT_SIZE) -> tuple[list, bool]:
    """Truncate results to fit within output size limit.

    Returns:
        Tuple of (truncated_results, was_truncated)
    """
    truncated = []
    total_size = 0
    was_truncated = False

    for result in results:
        # Truncate individual result if too large
        result_str = json.dumps(result)
        if len(result_str) > MAX_RESULT_SIZE:
            # Keep all fields but truncate long string values
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

        # Check if adding this result exceeds total size
        if total_size + len(result_str) > max_output_size:
            was_truncated = True
            break

        truncated.append(result)
        total_size += len(result_str)

    return truncated, was_truncated


class SplunkSearchArgs(BaseModel):
    """Arguments for search_splunk tool."""
    query: str = Field(description="SPL query to execute (e.g., 'index=main error | stats count by host')")
    earliest_time: str = Field(default="-1h", description="Start time (e.g., '-1h', '-24h', '-7d')")
    latest_time: str = Field(default="now", description="End time (default: 'now')")
    max_count: int = Field(default=100, description="Maximum results to return (default: 100)")


class SplunkListIndexesArgs(BaseModel):
    """Arguments for list_splunk_indexes tool."""
    pass


class SplunkListSourcetypesArgs(BaseModel):
    """Arguments for list_splunk_sourcetypes tool."""
    index: Optional[str] = Field(default=None, description="Optional: filter by specific index")


def _get_splunk_credentials(user_id: str) -> Optional[Dict[str, Any]]:
    """Get Splunk credentials for a user."""
    try:
        creds = get_token_data(user_id, "splunk")
        if not creds:
            return None
        api_token = creds.get("api_token")
        base_url = creds.get("base_url")
        if not api_token or not base_url:
            return None
        return {"base_url": base_url, "api_token": api_token}
    except Exception as exc:
        logger.error(f"[SPLUNK-TOOL] Failed to get credentials: {exc}")
        return None


def _splunk_headers(api_token: str) -> Dict[str, str]:
    """Return headers for Splunk API requests."""
    return {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }


def is_splunk_connected(user_id: str) -> bool:
    """Check if Splunk is connected for a user."""
    return _get_splunk_credentials(user_id) is not None


def search_splunk(
    query: str,
    earliest_time: str = "-1h",
    latest_time: str = "now",
    max_count: int = 100,
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    """Execute an SPL query against Splunk and return results.

    Args:
        query: SPL query to execute
        earliest_time: Start time (e.g., '-1h', '-24h')
        latest_time: End time (default: 'now')
        max_count: Maximum results to return
        user_id: User ID (injected by context)

    Returns:
        JSON string with search results or error message
    """
    if not user_id:
        return json.dumps({"error": "User context not available"})

    creds = _get_splunk_credentials(user_id)
    if not creds:
        return json.dumps({"error": "Splunk not connected. Please connect Splunk first."})

    # Ensure query starts with 'search' if needed
    search_query = query.strip()
    if not search_query.startswith("|") and not search_query.lower().startswith("search"):
        search_query = f"search {search_query}"

    logger.info(f"[SPLUNK-TOOL] Executing search for user {user_id}: {search_query[:100]}...")

    try:
        url = f"{creds['base_url']}/services/search/jobs/export"
        payload = {
            "search": search_query,
            "earliest_time": earliest_time,
            "latest_time": latest_time,
            "output_mode": "json",
            "count": min(max_count, MAX_SEARCH_RESULTS_CAP),
        }

        response = requests.post(
            url,
            headers=_splunk_headers(creds["api_token"]),
            data=payload,
            timeout=SPLUNK_SEARCH_TIMEOUT,
            verify=False,
        )

        if response.status_code == 401:
            return json.dumps({"error": "Splunk authentication failed. Token may be expired."})
        elif response.status_code == 400:
            error_msg = response.text[:200] if response.text else "Bad request"
            return json.dumps({"error": f"Invalid SPL query: {error_msg}"})

        response.raise_for_status()

        # Parse NDJSON response
        results = []
        for line in response.text.strip().split("\n"):
            if line:
                try:
                    obj = json.loads(line)
                    if "result" in obj:
                        results.append(obj["result"])
                    elif "results" in obj:
                        results.extend(obj["results"])
                except json.JSONDecodeError:
                    continue

        # Truncate results if too many
        if len(results) > max_count:
            results = results[:max_count]

        # Truncate to fit output size limit
        original_count = len(results)
        results, was_truncated = _truncate_results(results)

        response_data = {
            "success": True,
            "query": search_query,
            "time_range": f"{earliest_time} to {latest_time}",
            "result_count": len(results),
            "results": results,
        }

        if was_truncated:
            response_data["truncated"] = True
            response_data["note"] = f"Results truncated from {original_count} to {len(results)} due to size limit. Use more specific query or add '| head N' to limit results."

        return json.dumps(response_data)

    except requests.exceptions.Timeout:
        return json.dumps({"error": "Search timed out. Try a narrower time range or simpler query."})
    except requests.exceptions.RequestException as exc:
        logger.error(f"[SPLUNK-TOOL] Search failed: {exc}")
        return json.dumps({"error": f"Search failed: {str(exc)}"})


def list_splunk_indexes(user_id: Optional[str] = None, **kwargs) -> str:
    """List available Splunk indexes.

    Args:
        user_id: User ID (injected by context)

    Returns:
        JSON string with list of indexes
    """
    if not user_id:
        return json.dumps({"error": "User context not available"})

    creds = _get_splunk_credentials(user_id)
    if not creds:
        return json.dumps({"error": "Splunk not connected"})

    logger.info(f"[SPLUNK-TOOL] Listing indexes for user {user_id}")

    try:
        url = f"{creds['base_url']}/services/data/indexes"
        headers = _splunk_headers(creds["api_token"])
        headers["Accept"] = "application/json"

        response = requests.get(
            url,
            headers=headers,
            params={"output_mode": "json", "count": MAX_INDEXES_RETURN},
            timeout=30,
            verify=False,
        )

        if response.status_code == 401:
            return json.dumps({"error": "Authentication failed"})

        response.raise_for_status()
        data = response.json()

        indexes = []
        for entry in data.get("entry", []):
            name = entry.get("name", "")
            content = entry.get("content", {})
            # Skip internal indexes
            if name.startswith("_") and name not in ["_internal", "_audit"]:
                continue
            indexes.append({
                "name": name,
                "totalEventCount": content.get("totalEventCount", 0),
                "disabled": content.get("disabled", False),
            })

        # Sort by event count (most active first)
        indexes.sort(key=lambda x: x["totalEventCount"], reverse=True)

        return json.dumps({
            "success": True,
            "index_count": len(indexes),
            "indexes": indexes,
        })

    except requests.exceptions.RequestException as exc:
        logger.error(f"[SPLUNK-TOOL] Failed to list indexes: {exc}")
        return json.dumps({"error": f"Failed to list indexes: {str(exc)}"})


def list_splunk_sourcetypes(
    index: Optional[str] = None,
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    """List available Splunk sourcetypes.

    Args:
        index: Optional index to filter by
        user_id: User ID (injected by context)

    Returns:
        JSON string with list of sourcetypes
    """
    if not user_id:
        return json.dumps({"error": "User context not available"})

    creds = _get_splunk_credentials(user_id)
    if not creds:
        return json.dumps({"error": "Splunk not connected"})

    # Validate index to prevent SPL injection
    if index:
        is_valid, error_msg = _validate_splunk_identifier(index, "index")
        if not is_valid:
            logger.warning(f"[SPLUNK-TOOL] Invalid index parameter: {error_msg}")
            return json.dumps({"error": error_msg})

    logger.info(f"[SPLUNK-TOOL] Listing sourcetypes for user {user_id}, index={index}")

    try:
        # Use metadata search to get sourcetypes
        if index:
            search_query = f"| metadata type=sourcetypes index={index} | table sourcetype totalCount"
        else:
            search_query = "| metadata type=sourcetypes | table sourcetype totalCount"

        url = f"{creds['base_url']}/services/search/jobs/export"
        payload = {
            "search": search_query,
            "earliest_time": "-7d",
            "latest_time": "now",
            "output_mode": "json",
        }

        response = requests.post(
            url,
            headers=_splunk_headers(creds["api_token"]),
            data=payload,
            timeout=30,
            verify=False,
        )

        if response.status_code == 401:
            return json.dumps({"error": "Authentication failed"})

        response.raise_for_status()

        sourcetypes = []
        for line in response.text.strip().split("\n"):
            if line:
                try:
                    obj = json.loads(line)
                    if "result" in obj:
                        result = obj["result"]
                        sourcetypes.append({
                            "sourcetype": result.get("sourcetype", ""),
                            "totalCount": int(result.get("totalCount", 0)),
                        })
                except (json.JSONDecodeError, ValueError):
                    continue

        # Sort by count (most common first)
        sourcetypes.sort(key=lambda x: x["totalCount"], reverse=True)

        return json.dumps({
            "success": True,
            "sourcetype_count": len(sourcetypes),
            "index_filter": index,
            "sourcetypes": sourcetypes[:MAX_SOURCETYPES_RETURN],
        })

    except requests.exceptions.RequestException as exc:
        logger.error(f"[SPLUNK-TOOL] Failed to list sourcetypes: {exc}")
        return json.dumps({"error": f"Failed to list sourcetypes: {str(exc)}"})
