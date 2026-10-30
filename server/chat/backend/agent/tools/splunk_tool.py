"""Splunk SPL search tool for RCA agent."""

import json
import logging
import re
from typing import Any, Dict, Optional

import requests
from pydantic import BaseModel, Field

from utils.splunk_accounts import SplunkAccessError, list_splunk_accounts, resolve_targets

logger = logging.getLogger(__name__)

SPLUNK_SEARCH_TIMEOUT = 60

from utils.splunk_config import SPLUNK_SSL_VERIFY

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
    account_id: Optional[str] = Field(
        default=None,
        description=(
            "Splunk to query, as host:port (for example prod.splunkcloud.com:8089). "
            "Omit to query every Splunk this user can access."
        ),
    )


class SplunkListIndexesArgs(BaseModel):
    """Arguments for list_splunk_indexes tool."""
    account_id: Optional[str] = Field(
        default=None,
        description="Splunk to list, as host:port. Omit to list indexes on every Splunk this user can access.",
    )


class SplunkListSourcetypesArgs(BaseModel):
    """Arguments for list_splunk_sourcetypes tool."""
    index: Optional[str] = Field(default=None, description="Optional: filter by specific index")
    account_id: Optional[str] = Field(
        default=None,
        description="Splunk to list, as host:port. Omit to list sourcetypes on every Splunk this user can access.",
    )


def _tool_targets(user_id: str, account_id: Optional[str]):
    """Granted Splunks for this call, or a JSON error string."""
    accounts = list_splunk_accounts(user_id)
    if not accounts:
        return None, json.dumps({"error": "Splunk not connected. Please connect Splunk first."})
    try:
        targets = resolve_targets(user_id, accounts, account_id)
    except SplunkAccessError as exc:
        body = {"error": exc.message}
        if exc.code:
            body["code"] = exc.code
        return None, json.dumps(body)
    if not targets:
        return None, json.dumps({"error": "Splunk not connected. Please connect Splunk first."})
    return targets, None


def _account_fields(account: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "account_id": account.get("account_id"),
        "server_name": account.get("server_name"),
        "base_url": account.get("base_url"),
    }


def _instances(targets: list) -> list:
    """Instances this call actually talked to, in target order."""
    return [_account_fields(account) for account in targets]


def _annotate(result: Dict[str, Any], targets: list) -> Dict[str, Any]:
    """Put the instance list first so a truncated tool excerpt still names it."""
    annotated: Dict[str, Any] = {"instances": _instances(targets)}
    # One target also carries the flat fields older readers already expect.
    if len(targets) == 1:
        annotated.update(_account_fields(targets[0]))
    annotated.update(result)
    return annotated


def _by_account(targets: list, run) -> Dict[str, Any]:
    """Run one call per granted Splunk. A slow instance does not block the others."""
    import contextvars
    from concurrent.futures import ThreadPoolExecutor

    def one(account):
        try:
            item = run(account)
        # One instance's failure must not drop the results from the others.
        except Exception:
            logger.exception("[SPLUNK-TOOL] Instance call failed")
            item = {"error": "Splunk request failed"}
        # A non-dict would fail the whole fan-out on the update below.
        if not isinstance(item, dict):
            item = {"error": "Splunk request failed"}
        item.update(_account_fields(account))
        return account["account_id"], item

    # A Context cannot be entered on two threads at once, so each worker copies it.
    def guarded(account):
        return contextvars.copy_context().run(one, account)

    workers = min(4, len(targets))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(pool.map(guarded, targets))


def _splunk_headers(api_token: str) -> Dict[str, str]:
    """Return headers for Splunk API requests."""
    return {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }


def is_splunk_connected(user_id: str) -> bool:
    """True when this user can query at least one connected Splunk."""
    targets, error = _tool_targets(user_id, None)
    return error is None and bool(targets)


def search_splunk(
    query: str,
    earliest_time: str = "-1h",
    latest_time: str = "now",
    max_count: int = 100,
    account_id: Optional[str] = None,
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

    targets, error = _tool_targets(user_id, account_id)
    if error:
        return error
    # Fan-out shares one cap so four instances cannot return four times 2MB.
    budget = max(1, MAX_OUTPUT_SIZE // len(targets))
    # Several granted Splunks — same shape as an AWS fan-out, keyed by account id.
    if len(targets) > 1:
        by_account = _by_account(
            targets,
            lambda account: _search_one(
                account, query, earliest_time, latest_time, max_count, budget
            ),
        )
        return json.dumps({
            "success": any(item.get("success") for item in by_account.values()),
            "instances": _instances(targets),
            "results_by_account": by_account,
            "note": "Queried every Splunk this user can access. Pass account_id to target one instance.",
        })

    result = _search_one(targets[0], query, earliest_time, latest_time, max_count, budget)
    return json.dumps(_annotate(result, targets))


def _ensure_search_prefix(query: str) -> str:
    # A generating command already starts with | or search.
    search_query = query.strip()
    if search_query.startswith("|") or search_query.lower().startswith("search"):
        return search_query
    return f"search {search_query}"


def _export_results(text: str) -> list:
    results = []
    for line in text.strip().split("\n"):
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "result" in obj:
            results.append(obj["result"])
        elif "results" in obj:
            results.extend(obj["results"])
    return results


def _search_http_error(response: requests.Response) -> Optional[Dict[str, Any]]:
    if response.status_code == 401:
        return {"error": "Splunk authentication failed. Token may be expired."}
    if response.status_code == 400:
        error_msg = response.text[:200] if response.text else "Bad request"
        return {"error": f"Invalid SPL query: {error_msg}"}
    return None


def _search_one(
    creds: Dict[str, Any],
    query: str,
    earliest_time: str,
    latest_time: str,
    max_count: int,
    output_budget: int = MAX_OUTPUT_SIZE,
) -> Dict[str, Any]:
    search_query = _ensure_search_prefix(query)
    # The account id lives on the same dict as the token, so it is not logged.
    logger.info("[SPLUNK-TOOL] Executing search")

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
            verify=SPLUNK_SSL_VERIFY,
        )

        http_error = _search_http_error(response)
        if http_error:
            return http_error

        response.raise_for_status()
        results = _export_results(response.text)
        if len(results) > max_count:
            results = results[:max_count]

        original_count = len(results)
        results, was_truncated = _truncate_results(results, output_budget)
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
        return response_data

    except requests.exceptions.Timeout:
        return {"error": "Search timed out. Try a narrower time range or simpler query."}
    except requests.exceptions.RequestException:
        logger.exception("[SPLUNK-TOOL] Search failed")
        return {"error": "Search failed"}


def list_splunk_indexes(account_id: Optional[str] = None, user_id: Optional[str] = None, **kwargs) -> str:
    """List available Splunk indexes.

    Args:
        user_id: User ID (injected by context)

    Returns:
        JSON string with list of indexes
    """
    if not user_id:
        return json.dumps({"error": "User context not available"})

    targets, error = _tool_targets(user_id, account_id)
    if error:
        return error
    # Several granted Splunks — list each one instead of mixing their indexes.
    if len(targets) > 1:
        by_account = _by_account(targets, _list_indexes_one)
        return json.dumps({
            "success": any(item.get("success") for item in by_account.values()),
            "instances": _instances(targets),
            "results_by_account": by_account,
            "note": "Listed indexes on every Splunk this user can access. Pass account_id to target one instance.",
        })

    result = _list_indexes_one(targets[0])
    return json.dumps(_annotate(result, targets))


def _list_indexes_one(creds: Dict[str, Any]) -> Dict[str, Any]:
    try:
        url = f"{creds['base_url']}/services/data/indexes"
        headers = _splunk_headers(creds["api_token"])
        headers["Accept"] = "application/json"

        response = requests.get(
            url,
            headers=headers,
            params={"output_mode": "json", "count": MAX_INDEXES_RETURN},
            timeout=30,
            verify=SPLUNK_SSL_VERIFY,
        )

        if response.status_code == 401:
            return {"error": "Authentication failed"}

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

        return {
            "success": True,
            "index_count": len(indexes),
            "indexes": indexes,
        }

    except requests.exceptions.RequestException:
        logger.exception("[SPLUNK-TOOL] Failed to list indexes")
        return {"error": "Failed to list indexes"}


def list_splunk_sourcetypes(
    index: Optional[str] = None,
    account_id: Optional[str] = None,
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

    # Validate index to prevent SPL injection
    if index:
        is_valid, error_msg = _validate_splunk_identifier(index, "index")
        if not is_valid:
            logger.warning("[SPLUNK-TOOL] Invalid index parameter: %s", error_msg)
            return json.dumps({"error": error_msg})

    targets, error = _tool_targets(user_id, account_id)
    if error:
        return error
    if len(targets) > 1:
        by_account = _by_account(targets, lambda account: _list_sourcetypes_one(account, index))
        return json.dumps({
            "success": any(item.get("success") for item in by_account.values()),
            "instances": _instances(targets),
            "results_by_account": by_account,
            "note": "Listed sourcetypes on every Splunk this user can access. Pass account_id to target one instance.",
        })

    result = _list_sourcetypes_one(targets[0], index)
    return json.dumps(_annotate(result, targets))


def _list_sourcetypes_one(creds: Dict[str, Any], index: Optional[str]) -> Dict[str, Any]:
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
            verify=SPLUNK_SSL_VERIFY,
        )

        if response.status_code == 401:
            return {"error": "Authentication failed"}

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

        return {
            "success": True,
            "sourcetype_count": len(sourcetypes),
            "index_filter": index,
            "sourcetypes": sourcetypes[:MAX_SOURCETYPES_RETURN],
        }

    except requests.exceptions.RequestException:
        logger.exception("[SPLUNK-TOOL] Failed to list sourcetypes")
        return {"error": "Failed to list sourcetypes"}
