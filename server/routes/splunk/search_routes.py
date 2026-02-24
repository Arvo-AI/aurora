"""Splunk search routes for running SPL queries."""

import logging
import re
from typing import Any, Dict, Optional, Tuple

import requests
from flask import Blueprint, Response, jsonify, request, stream_with_context

from utils.web.cors_utils import create_cors_response
from utils.auth.stateless_auth import get_user_id_from_request
from utils.auth.token_management import get_token_data

SPLUNK_TIMEOUT = 30
SPLUNK_SEARCH_TIMEOUT = 120

# Regex for valid Splunk SID: alphanumerics, underscores, hyphens, dots
SID_PATTERN = re.compile(r"^[a-zA-Z0-9_.\-]+$")

logger = logging.getLogger(__name__)

search_bp = Blueprint("splunk_search", __name__)


def _validate_sid(sid: str) -> Tuple[bool, Optional[str]]:
    """Validate Splunk search job ID format."""
    if not sid:
        return False, "SID is required"
    if len(sid) > 256:
        return False, "SID exceeds maximum length"
    if not SID_PATTERN.match(sid):
        return False, "SID contains invalid characters"
    return True, None


def _get_splunk_client_for_user(user_id: str) -> Optional[Dict[str, Any]]:
    """Get Splunk credentials for the user."""
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
        logger.error(f"[SPLUNK-SEARCH] Failed to get credentials for user {user_id}: {exc}")
        return None


def _splunk_headers(api_token: str) -> Dict[str, str]:
    """Return headers for Splunk API requests."""
    return {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }


@search_bp.route("/search", methods=["POST", "OPTIONS"])
def search_sync():
    """Execute a synchronous SPL search (oneshot mode)."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    creds = _get_splunk_client_for_user(user_id)
    if not creds:
        return jsonify({"error": "Splunk not connected"}), 400

    data = request.get_json(silent=True) or {}
    search_query = data.get("query") or data.get("search")
    earliest_time = data.get("earliestTime", "-24h")
    latest_time = data.get("latestTime", "now")
    max_count = data.get("maxCount", 1000)

    if not search_query:
        return jsonify({"error": "Search query is required"}), 400

    # Ensure query starts with 'search' command if not a generating command
    if not search_query.strip().startswith("|") and not search_query.strip().lower().startswith("search"):
        search_query = f"search {search_query}"

    logger.info(f"[SPLUNK-SEARCH] User {user_id} executing sync search: {search_query[:100]}...")

    try:
        # Use export endpoint with oneshot mode for streaming results
        url = f"{creds['base_url']}/services/search/jobs/export"
        payload = {
            "search": search_query,
            "earliest_time": earliest_time,
            "latest_time": latest_time,
            "output_mode": "json",
            "count": max_count,
        }

        response = requests.post(
            url,
            headers=_splunk_headers(creds["api_token"]),
            data=payload,
            timeout=SPLUNK_SEARCH_TIMEOUT,
            verify=False,
            stream=False,
        )

        if response.status_code == 401:
            return jsonify({"error": "Splunk authentication failed. Check your API token."}), 401
        elif response.status_code == 400:
            error_msg = response.text[:500] if response.text else "Bad request"
            return jsonify({"error": f"Invalid search query: {error_msg}"}), 400

        response.raise_for_status()

        # Parse NDJSON response (newline-delimited JSON)
        # Limit response size to prevent memory issues
        MAX_RESPONSE_SIZE = 10 * 1024 * 1024  # 10MB
        if len(response.text) > MAX_RESPONSE_SIZE:
            logger.warning(f"[SPLUNK-SEARCH] Response too large ({len(response.text)} bytes) for user {user_id}, truncating")
            response_text = response.text[:MAX_RESPONSE_SIZE]
        else:
            response_text = response.text

        results = []
        for line in response_text.strip().split("\n"):
            if line:
                try:
                    import json
                    obj = json.loads(line)
                    if "result" in obj:
                        results.append(obj["result"])
                    elif "results" in obj:
                        results.extend(obj["results"])
                except json.JSONDecodeError as e:
                    logger.debug(f"[SPLUNK-SEARCH] Skipping malformed NDJSON line: {e}")
                    continue

        return jsonify({
            "success": True,
            "results": results,
            "count": len(results),
        })

    except requests.exceptions.Timeout:
        logger.error(f"[SPLUNK-SEARCH] Search timeout for user {user_id}")
        return jsonify({"error": "Search timed out. Try a narrower time range or simpler query."}), 504
    except requests.exceptions.RequestException as exc:
        logger.error(f"[SPLUNK-SEARCH] Search failed for user {user_id}: {exc}", exc_info=True)
        return jsonify({"error": "Search request failed"}), 502


@search_bp.route("/search/jobs", methods=["POST", "OPTIONS"])
def create_search_job():
    """Create an asynchronous search job."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    creds = _get_splunk_client_for_user(user_id)
    if not creds:
        return jsonify({"error": "Splunk not connected"}), 400

    data = request.get_json(silent=True) or {}
    search_query = data.get("query") or data.get("search")
    earliest_time = data.get("earliestTime", "-24h")
    latest_time = data.get("latestTime", "now")

    if not search_query:
        return jsonify({"error": "Search query is required"}), 400

    # Ensure query starts with 'search' command if not a generating command
    if not search_query.strip().startswith("|") and not search_query.strip().lower().startswith("search"):
        search_query = f"search {search_query}"

    logger.info(f"[SPLUNK-SEARCH] User {user_id} creating async job: {search_query[:100]}...")

    try:
        url = f"{creds['base_url']}/services/search/v2/jobs"
        payload = {
            "search": search_query,
            "earliest_time": earliest_time,
            "latest_time": latest_time,
            "output_mode": "json",
        }

        response = requests.post(
            url,
            headers=_splunk_headers(creds["api_token"]),
            data=payload,
            timeout=SPLUNK_TIMEOUT,
            verify=False,
        )

        if response.status_code == 401:
            return jsonify({"error": "Splunk authentication failed"}), 401
        elif response.status_code == 400:
            error_msg = response.text[:500] if response.text else "Bad request"
            return jsonify({"error": f"Invalid search query: {error_msg}"}), 400

        response.raise_for_status()
        result = response.json()

        # Extract SID from response
        sid = result.get("sid") or result.get("entry", [{}])[0].get("content", {}).get("sid")

        if not sid:
            logger.error(f"[SPLUNK-SEARCH] Failed to extract SID from response for user {user_id}")
            return jsonify({"success": False, "error": "Failed to extract job ID from Splunk response"}), 500

        return jsonify({
            "success": True,
            "sid": sid,
            "message": "Search job created",
        })

    except requests.exceptions.RequestException as exc:
        logger.error(f"[SPLUNK-SEARCH] Job creation failed for user {user_id}: {exc}", exc_info=True)
        return jsonify({"error": "Failed to create search job"}), 502


@search_bp.route("/search/jobs/<sid>", methods=["GET", "OPTIONS"])
def get_job_status(sid: str):
    """Get the status of a search job."""
    if request.method == "OPTIONS":
        return create_cors_response()

    # Validate SID format
    valid, error = _validate_sid(sid)
    if not valid:
        return jsonify({"error": error}), 400

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    creds = _get_splunk_client_for_user(user_id)
    if not creds:
        return jsonify({"error": "Splunk not connected"}), 400

    try:
        url = f"{creds['base_url']}/services/search/v2/jobs/{sid}"
        headers = _splunk_headers(creds["api_token"])
        headers["Accept"] = "application/json"

        response = requests.get(
            url,
            headers=headers,
            params={"output_mode": "json"},
            timeout=SPLUNK_TIMEOUT,
            verify=False,
        )

        if response.status_code == 404:
            return jsonify({"error": "Search job not found"}), 404

        response.raise_for_status()
        result = response.json()

        # Extract job info
        entry = result.get("entry", [{}])[0]
        content = entry.get("content", {})

        return jsonify({
            "sid": sid,
            "dispatchState": content.get("dispatchState"),
            "isDone": content.get("isDone", False),
            "isFailed": content.get("isFailed", False),
            "resultCount": content.get("resultCount", 0),
            "scanCount": content.get("scanCount", 0),
            "eventCount": content.get("eventCount", 0),
            "doneProgress": content.get("doneProgress", 0),
            "runDuration": content.get("runDuration"),
        })

    except requests.exceptions.RequestException as exc:
        logger.error(f"[SPLUNK-SEARCH] Failed to get job status for {sid}: {exc}", exc_info=True)
        return jsonify({"error": "Failed to get job status"}), 502


@search_bp.route("/search/jobs/<sid>/results", methods=["GET", "OPTIONS"])
def get_job_results(sid: str):
    """Get the results of a completed search job."""
    if request.method == "OPTIONS":
        return create_cors_response()

    # Validate SID format
    valid, error = _validate_sid(sid)
    if not valid:
        return jsonify({"error": error}), 400

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    creds = _get_splunk_client_for_user(user_id)
    if not creds:
        return jsonify({"error": "Splunk not connected"}), 400

    offset = request.args.get("offset", 0, type=int)
    count = request.args.get("count", 1000, type=int)

    try:
        url = f"{creds['base_url']}/services/search/v2/jobs/{sid}/results"
        headers = _splunk_headers(creds["api_token"])
        headers["Accept"] = "application/json"

        response = requests.get(
            url,
            headers=headers,
            params={
                "output_mode": "json",
                "offset": offset,
                "count": count,
            },
            timeout=SPLUNK_SEARCH_TIMEOUT,
            verify=False,
        )

        if response.status_code == 404:
            return jsonify({"error": "Search job not found or results not ready"}), 404
        elif response.status_code == 204:
            return jsonify({"results": [], "count": 0, "offset": offset})

        response.raise_for_status()
        result = response.json()

        results = result.get("results", [])

        return jsonify({
            "results": results,
            "count": len(results),
            "offset": offset,
        })

    except requests.exceptions.RequestException as exc:
        logger.error(f"[SPLUNK-SEARCH] Failed to get job results for {sid}: {exc}", exc_info=True)
        return jsonify({"error": "Failed to get search results"}), 502


@search_bp.route("/search/jobs/<sid>", methods=["DELETE", "OPTIONS"])
def cancel_job(sid: str):
    """Cancel a running search job."""
    if request.method == "OPTIONS":
        return create_cors_response()

    # Validate SID format
    valid, error = _validate_sid(sid)
    if not valid:
        return jsonify({"error": error}), 400

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    creds = _get_splunk_client_for_user(user_id)
    if not creds:
        return jsonify({"error": "Splunk not connected"}), 400

    try:
        url = f"{creds['base_url']}/services/search/v2/jobs/{sid}/control"
        response = requests.post(
            url,
            headers=_splunk_headers(creds["api_token"]),
            data={"action": "cancel"},
            timeout=SPLUNK_TIMEOUT,
            verify=False,
        )

        if response.status_code == 404:
            return jsonify({"error": "Search job not found"}), 404

        response.raise_for_status()

        return jsonify({
            "success": True,
            "message": "Job cancelled",
        })

    except requests.exceptions.RequestException as exc:
        logger.error(f"[SPLUNK-SEARCH] Failed to cancel job {sid}: {exc}", exc_info=True)
        return jsonify({"error": "Failed to cancel job"}), 502
