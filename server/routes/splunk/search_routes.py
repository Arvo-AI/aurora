"""Splunk search routes for running SPL queries."""

import logging
import re
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote

import requests
from flask import Blueprint, Response, jsonify, request, stream_with_context

from utils.auth.rbac_decorators import require_permission
from utils.log_sanitizer import hash_for_log, sanitize
from utils.splunk_accounts import SplunkAccessError, list_splunk_accounts, resolve_targets

SPLUNK_TIMEOUT = 30
SPLUNK_SEARCH_TIMEOUT = 120

from utils.splunk_config import SPLUNK_SSL_VERIFY

# Regex for valid Splunk SID: alphanumerics, underscores, hyphens, dots
SID_PATTERN = re.compile(r"^[a-zA-Z0-9_.\-]+$")

logger = logging.getLogger(__name__)

_NOT_CONNECTED = "Splunk not connected"

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


def _selector_from_request() -> Optional[str]:
    selector = request.args.get("account")
    if selector:
        return selector
    # GET has no body. A posted search names the instance as accountId.
    if request.method == "GET":
        return None
    data = request.get_json(silent=True) or {}
    return data.get("accountId") or data.get("account")


def _get_splunk_client_for_user(user_id: str):
    """Credentials for one granted instance, or a Flask error response."""
    try:
        accounts = list_splunk_accounts(user_id, raise_on_error=True)
    # A Vault error is retryable. An empty list is "not connected".
    except Exception:
        logger.exception("[SPLUNK-SEARCH] Failed to get credentials for user %s", hash_for_log(user_id))
        return None, (jsonify({"error": "Failed to load Splunk credentials"}), 503)
    if not accounts:
        return None, (jsonify({"error": _NOT_CONNECTED}), 400)
    try:
        targets = resolve_targets(user_id, accounts, _selector_from_request())
    except SplunkAccessError as exc:
        # Not granted — 403. An account id this org never connected — 400.
        status = 403 if exc.code == "CONNECTOR_DENIED" else 400
        body = {"error": exc.message}
        if exc.code:
            body["code"] = exc.code
        return None, (jsonify(body), status)
    if not targets:
        return None, (jsonify({"error": _NOT_CONNECTED}), 400)
    # Search jobs live on one Splunk. No selector uses the first granted instance.
    account = targets[0]
    return {"base_url": account["base_url"], "api_token": account["api_token"], "account_id": account["account_id"]}, None


def _splunk_headers(api_token: str) -> Dict[str, str]:
    """Return headers for Splunk API requests."""
    return {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }


@search_bp.route("/search", methods=["POST"])
@require_permission("connectors", "read")
def search_sync(user_id):
    """Execute a synchronous SPL search (oneshot mode)."""
    creds, error = _get_splunk_client_for_user(user_id)
    if error:
        return error

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

    logger.info(f"[SPLUNK-SEARCH] User {sanitize(user_id)} executing sync search: {sanitize(search_query)[:100]}...")

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
            verify=SPLUNK_SSL_VERIFY,
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
            logger.warning(f"[SPLUNK-SEARCH] Response too large ({len(response.text)} bytes) for user {sanitize(user_id)}, truncating")
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
        logger.error(f"[SPLUNK-SEARCH] Search timeout for user {sanitize(user_id)}")
        return jsonify({"error": "Search timed out. Try a narrower time range or simpler query."}), 504
    except requests.exceptions.RequestException as exc:
        logger.error(f"[SPLUNK-SEARCH] Search failed for user {sanitize(user_id)}: {sanitize(exc)}", exc_info=True)
        return jsonify({"error": "Search request failed"}), 502


@search_bp.route("/search/jobs", methods=["POST"])
@require_permission("connectors", "read")
def create_search_job(user_id):
    """Create an asynchronous search job."""
    creds, error = _get_splunk_client_for_user(user_id)
    if error:
        return error

    data = request.get_json(silent=True) or {}
    search_query = data.get("query") or data.get("search")
    earliest_time = data.get("earliestTime", "-24h")
    latest_time = data.get("latestTime", "now")

    if not search_query:
        return jsonify({"error": "Search query is required"}), 400

    # Ensure query starts with 'search' command if not a generating command
    if not search_query.strip().startswith("|") and not search_query.strip().lower().startswith("search"):
        search_query = f"search {search_query}"

    logger.info(f"[SPLUNK-SEARCH] User {sanitize(user_id)} creating async job: {sanitize(search_query)[:100]}...")

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
            verify=SPLUNK_SSL_VERIFY,
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
            logger.error(f"[SPLUNK-SEARCH] Failed to extract SID from response for user {sanitize(user_id)}")
            return jsonify({"success": False, "error": "Failed to extract job ID from Splunk response"}), 500

        return jsonify({
            "success": True,
            "sid": sid,
            "message": "Search job created",
        })

    except requests.exceptions.RequestException as exc:
        logger.error(f"[SPLUNK-SEARCH] Job creation failed for user {sanitize(user_id)}: {sanitize(exc)}", exc_info=True)
        return jsonify({"error": "Failed to create search job"}), 502


@search_bp.route("/search/jobs/<sid>", methods=["GET"])
@require_permission("connectors", "read")
def get_job_status(user_id, sid: str):
    """Get the status of a search job."""
    # Validate SID format
    valid, error = _validate_sid(sid)
    if not valid:
        return jsonify({"error": error}), 400

    creds, error = _get_splunk_client_for_user(user_id)
    if error:
        return error

    try:
        url = f"{creds['base_url']}/services/search/v2/jobs/{quote(sid, safe='')}"
        headers = _splunk_headers(creds["api_token"])
        headers["Accept"] = "application/json"

        response = requests.get(
            url,
            headers=headers,
            params={"output_mode": "json"},
            timeout=SPLUNK_TIMEOUT,
            verify=SPLUNK_SSL_VERIFY,
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


@search_bp.route("/search/jobs/<sid>/results", methods=["GET"])
@require_permission("connectors", "read")
def get_job_results(user_id, sid: str):
    """Get the results of a completed search job."""
    # Validate SID format
    valid, error = _validate_sid(sid)
    if not valid:
        return jsonify({"error": error}), 400

    creds, error = _get_splunk_client_for_user(user_id)
    if error:
        return error

    offset = request.args.get("offset", 0, type=int)
    count = request.args.get("count", 1000, type=int)

    try:
        url = f"{creds['base_url']}/services/search/v2/jobs/{quote(sid, safe='')}/results"
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
            verify=SPLUNK_SSL_VERIFY,
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


@search_bp.route("/search/jobs/<sid>", methods=["DELETE"])
@require_permission("connectors", "write")
def cancel_job(user_id, sid: str):
    """Cancel a running search job."""
    # Validate SID format
    valid, error = _validate_sid(sid)
    if not valid:
        return jsonify({"error": error}), 400

    creds, error = _get_splunk_client_for_user(user_id)
    if error:
        return error

    try:
        url = f"{creds['base_url']}/services/search/v2/jobs/{quote(sid, safe='')}/control"
        response = requests.post(
            url,
            headers=_splunk_headers(creds["api_token"]),
            data={"action": "cancel"},
            timeout=SPLUNK_TIMEOUT,
            verify=SPLUNK_SSL_VERIFY,
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
