import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional

import requests
from flask import Blueprint, jsonify, request

from routes.splunk.tasks import process_splunk_alert
from utils.db.connection_pool import db_pool
from utils.web.public_url import external_backend_url
from utils.auth.stateless_auth import (
    get_org_id_from_request,
    get_user_preference,
    store_user_preference,
    set_rls_context,
)
from utils.auth.rbac_decorators import require_permission
from utils.log_sanitizer import sanitize, hash_for_log
from utils.secrets.secret_ref_utils import delete_user_secret
from utils.splunk_accounts import (
    account_allowed,
    account_id_for_url,
    account_summary,
    find_account,
    list_splunk_accounts,
    retire_splunk_connections,
    store_splunk_accounts,
    sync_splunk_connections,
    upsert_account,
)
SPLUNK_TIMEOUT = 15

from utils.splunk_config import SPLUNK_SSL_VERIFY

logger = logging.getLogger(__name__)

splunk_bp = Blueprint("splunk", __name__)


class SplunkAPIError(Exception):
    """Custom error for Splunk API interactions."""


class SplunkClient:
    """Client for interacting with Splunk REST API."""

    def __init__(self, base_url: str, api_token: str):
        self.base_url = base_url
        self.api_token = api_token

    @staticmethod
    def normalize_base_url(raw_url: str) -> Optional[str]:
        """Normalize and validate Splunk instance URL."""
        if not raw_url:
            return None

        url = raw_url.strip()
        if not url:
            return None

        if not re.match(r"^https?://", url, re.IGNORECASE):
            url = "https://" + url

        # Remove any trailing paths (like /en-US/ from web UI URLs)
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(url)
        url = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))

        # Validate URL format (allow various ports for Enterprise/Cloud)
        if not re.match(r"^https?://[A-Za-z0-9._-]+(:[0-9]{2,5})?$", url):
            return None

        return url

    @property
    def headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.base_url}{path}"
        try:
            response = requests.request(
                method, url, headers=self.headers, timeout=SPLUNK_TIMEOUT, verify=SPLUNK_SSL_VERIFY, **kwargs
            )
            response.raise_for_status()
            return response
        except requests.exceptions.Timeout as exc:
            logger.error(f"[SPLUNK] {method} {url} timeout: {exc}")
            raise SplunkAPIError("Connection timed out. Check if Splunk is reachable and port 8089 is open.") from exc
        except requests.exceptions.SSLError as exc:
            # SSLError must come before ConnectionError (it's a subclass)
            logger.error("[SPLUNK] %s SSL error: %s", method, type(exc).__name__)
            raise SplunkAPIError("SSL/TLS error — check SPLUNK_SSL_VERIFY config") from exc
        except requests.exceptions.ConnectionError as exc:
            logger.error(f"[SPLUNK] {method} {url} connection error: {exc}")
            error_str = str(exc).lower()
            if "name or service not known" in error_str or "nodename nor servname" in error_str:
                raise SplunkAPIError("DNS resolution failed. Check the instance URL.") from exc
            elif "connection refused" in error_str:
                raise SplunkAPIError("Connection refused. Ensure Splunk is running and port 8089 is accessible.") from exc
            else:
                raise SplunkAPIError("Unable to connect. Ensure Aurora has network access to your Splunk instance.") from exc
        except requests.HTTPError as exc:
            logger.error(f"[SPLUNK] {method} {url} failed: {exc}")
            raise SplunkAPIError(str(exc)) from exc
        except requests.RequestException as exc:
            logger.error(f"[SPLUNK] {method} {url} error: {exc}")
            raise SplunkAPIError("Unable to reach Splunk instance") from exc

    def get_server_info(self) -> Dict[str, Any]:
        """Fetch server info to validate connection."""
        return self._request("GET", "/services/server/info?output_mode=json").json()

    def get_current_user(self) -> Optional[Dict[str, Any]]:
        """Fetch current user context."""
        try:
            return self._request("GET", "/services/authentication/current-context?output_mode=json").json()
        except SplunkAPIError:
            logger.debug("[SPLUNK] Unable to fetch current user context", exc_info=True)
            return None


def _account_from_server(base_url: str, api_token: str, server_info: Dict[str, Any], user_context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    entry = server_info.get("entry", [{}])[0] if server_info.get("entry") else {}
    content = entry.get("content", {})
    user_entry = user_context.get("entry", [{}])[0] if user_context and user_context.get("entry") else {}
    user_content = user_entry.get("content", {})
    account_id = account_id_for_url(base_url)
    return {
        "account_id": account_id,
        "api_token": api_token,
        "base_url": base_url,
        "server_name": content.get("serverName", "Splunk"),
        "version": content.get("version"),
        "instance_type": content.get("instance_type", "enterprise"),
        "username": user_content.get("username"),
    }


def _status_summary(user_id: str, account: Dict[str, Any]) -> Dict[str, Any]:
    """Live check of one instance. A failure stays on that row so the others remain usable."""
    summary = account_summary(account)
    summary["allowed"] = account_allowed(user_id, account["account_id"])
    client = SplunkClient(account["base_url"], account["api_token"])
    try:
        server_info = client.get_server_info()
    except SplunkAPIError as exc:
        logger.warning(
            "[SPLUNK] Status check failed for %s: %s",
            sanitize(account.get("account_id")),
            sanitize(exc),
        )
        summary["valid"] = False
        summary["error"] = "Failed to validate stored Splunk credentials"
        return summary
    entry = server_info.get("entry", [{}])[0] if server_info.get("entry") else {}
    content = entry.get("content", {})
    # Refresh the label from the live server without rewriting the stored token.
    if content.get("serverName"):
        summary["serverName"] = content.get("serverName")
    if content.get("version"):
        summary["version"] = content.get("version")
    summary["instanceType"] = content.get("instance_type", account.get("instance_type") or "enterprise")
    summary["valid"] = True
    return summary


def _stored_payload(user_id: str, accounts: list, validated_id: Optional[str] = None) -> Dict[str, Any]:
    """Response from stored rows. Skips the live check so connect stays within one validation."""
    if not accounts:
        return {"connected": False, "accounts": []}
    summaries = []
    for account in accounts:
        summary = account_summary(account)
        summary["allowed"] = account_allowed(user_id, account["account_id"])
        # Other instances were not rechecked, so leave their validity unknown.
        if validated_id is not None and account["account_id"] == validated_id:
            summary["valid"] = True
        summaries.append(summary)
    primary = summaries[0]
    return {
        "connected": True,
        "baseUrl": primary.get("baseUrl"),
        "username": primary.get("username"),
        "server": {
            "name": primary.get("serverName"),
            "version": primary.get("version"),
            "instanceType": primary.get("instanceType") or "enterprise",
        },
        "accounts": summaries,
    }


def _connected_payload(user_id: str, accounts: list) -> Dict[str, Any]:
    if not accounts:
        return {"connected": False, "accounts": []}
    # One slow instance must not stack timeouts in front of the others.
    workers = min(4, len(accounts))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        summaries = list(pool.map(lambda account: _status_summary(user_id, account), accounts))
    primary = summaries[0]
    payload = {
        "connected": True,
        "baseUrl": primary.get("baseUrl"),
        "username": primary.get("username"),
        "server": {
            "name": primary.get("serverName"),
            "version": primary.get("version"),
            "instanceType": primary.get("instanceType") or "enterprise",
        },
        "accounts": summaries,
    }
    # Every stored token failed — still list them so they can be removed.
    if summaries and not any(item.get("valid") for item in summaries):
        payload["error"] = "Failed to validate stored Splunk credentials"
    return payload


@splunk_bp.route("/connect", methods=["POST"])
@require_permission("connectors", "write")
def connect(user_id):
    """Store a Splunk API token. A new host is added; the same host replaces its token."""
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    api_token = data.get("apiToken")
    raw_base_url = data.get("baseUrl")

    if not api_token or not isinstance(api_token, str):
        return jsonify({"error": "apiToken is required"}), 400

    base_url = SplunkClient.normalize_base_url(raw_base_url) if raw_base_url else None
    if not base_url:
        return jsonify({"error": "A valid Splunk instance URL is required (e.g., https://your-instance.splunkcloud.com:8089)"}), 400

    logger.info(f"[SPLUNK] Connecting user {sanitize(user_id)} to {sanitize(base_url)} (token_hash={hash_for_log(api_token)})")

    client = SplunkClient(base_url, api_token)

    try:
        server_info = client.get_server_info()
        user_context = client.get_current_user()
    except SplunkAPIError as exc:
        logger.error(f"[SPLUNK] Connection validation failed for user {sanitize(user_id)}: {exc}")
        return jsonify({"error": "Failed to validate Splunk credentials"}), 502

    incoming = _account_from_server(base_url, api_token, server_info, user_context)
    if not incoming.get("account_id"):
        return jsonify({"error": "A valid Splunk instance URL is required (e.g., https://your-instance.splunkcloud.com:8089)"}), 400

    existing = list_splunk_accounts(user_id)
    accounts, replaced = upsert_account(existing, incoming)

    try:
        store_splunk_accounts(user_id, accounts)
        logger.info(
            "[SPLUNK] Stored credentials for user %s (account=%s, accounts=%d, replaced=%s)",
            sanitize(user_id),
            sanitize(incoming["account_id"]),
            len(accounts),
            replaced,
        )
    except Exception as exc:
        logger.exception(f"[SPLUNK] Failed to store credentials for user {sanitize(user_id)}: {exc}")
        return jsonify({"error": "Failed to store Splunk credentials"}), 500

    payload = _stored_payload(user_id, accounts, validated_id=incoming["account_id"])
    payload["success"] = True
    payload["replaced"] = replaced
    return jsonify(payload)


@splunk_bp.route("/status", methods=["GET"])
@require_permission("connectors", "read")
def status(user_id):
    """Check every connected Splunk. One dead token does not hide the others."""
    accounts = list_splunk_accounts(user_id)
    if not accounts:
        return jsonify({"connected": False, "accounts": []})

    # Backfill connection rows for a Splunk saved before instances were tracked.
    try:
        sync_splunk_connections(user_id, accounts)
    except Exception:
        logger.warning("[SPLUNK] Could not sync connection rows for user %s", hash_for_log(user_id))

    return jsonify(_connected_payload(user_id, accounts))


@splunk_bp.route("/disconnect", methods=["POST", "DELETE"])
@require_permission("connectors", "write")
def disconnect(user_id):
    """Remove one Splunk (?account=host:port) or every Splunk when omitted."""
    selector = request.args.get("account") or (request.get_json(silent=True) or {}).get("accountId")
    try:
        accounts = list_splunk_accounts(user_id)
        # One instance — leave the rest stored and drop only that connection row.
        if selector and str(selector).strip():
            match = find_account(accounts, str(selector))
            if match is None:
                known = ", ".join(a["account_id"] for a in accounts) or "none"
                return jsonify({"error": f"Unknown Splunk instance. Connected: {known}"}), 404
            remaining = [a for a in accounts if a["account_id"] != match["account_id"]]
            if remaining:
                store_splunk_accounts(user_id, remaining)
                logger.info(
                    "[SPLUNK] Removed account %s for user %s (%d remaining)",
                    sanitize(match["account_id"]),
                    sanitize(user_id),
                    len(remaining),
                )
                payload = _stored_payload(user_id, remaining)
                payload["success"] = True
                payload["message"] = "Splunk instance removed"
                return jsonify(payload), 200

        retire_splunk_connections(user_id)
        success, deleted_count = delete_user_secret(user_id, "splunk")
        if not success:
            logger.warning("[SPLUNK] Failed to clean up secrets during disconnect")
            return jsonify({"success": False, "error": "Failed to delete stored credentials"}), 500

        logger.info("[SPLUNK] Disconnected provider (deleted %s token entries)", deleted_count)

        return jsonify({
            "success": True,
            "message": "Splunk disconnected successfully",
            "deleted": deleted_count,
            "connected": False,
            "accounts": [],
        }), 200

    except Exception as exc:
        logger.exception("[SPLUNK] Failed to disconnect provider")
        return jsonify({"error": "Failed to disconnect Splunk"}), 500


@splunk_bp.route("/alerts/webhook/<user_id>", methods=["POST"])
def alert_webhook(user_id: str):
    """Receive alert webhook from Splunk for a specific user."""
    if not user_id:
        logger.warning("[SPLUNK] Webhook received without user_id")
        return jsonify({"error": "user_id is required"}), 400

    # Check if user has Splunk connected
    if not list_splunk_accounts(user_id):
        logger.warning("[SPLUNK] Webhook received for user %s with no Splunk connection", sanitize(user_id))
        return jsonify({"error": "Splunk not connected for this user"}), 404

    payload = request.get_json(silent=True) or {}
    logger.info("[SPLUNK] Received alert webhook for user %s: %s", sanitize(user_id), sanitize(payload.get("search_name", "unknown")))

    # Sanitize headers - redact sensitive values
    sensitive_headers = {"authorization", "cookie", "set-cookie", "proxy-authorization", "x-api-key", "x-csrf-token"}
    sanitized_headers = {}
    for key, value in request.headers:
        key_lower = key.lower()
        if key_lower in sensitive_headers or "token" in key_lower or "secret" in key_lower:
            sanitized_headers[key] = "<REDACTED>"
        else:
            sanitized_headers[key] = value

    metadata = {
        "headers": sanitized_headers,
        "remote_addr": request.remote_addr,
    }

    process_splunk_alert.delay(payload, metadata, user_id)

    return jsonify({"received": True})


@splunk_bp.route("/alerts", methods=["GET"])
@require_permission("connectors", "read")
def get_alerts(user_id):
    """Fetch Splunk alerts for the authenticated user."""
    org_id = get_org_id_from_request()
    limit = request.args.get("limit", 50, type=int)
    offset = request.args.get("offset", 0, type=int)
    state_filter = request.args.get("state")

    try:
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()
            set_rls_context(cursor, conn, user_id, log_prefix="[Splunk]")

            if state_filter:
                cursor.execute(
                    """
                    SELECT id, alert_id, alert_title, alert_state, search_name,
                           search_query, result_count, severity, payload, received_at, created_at
                    FROM splunk_alerts
                    WHERE org_id = %s AND alert_state = %s
                    ORDER BY received_at DESC
                    LIMIT %s OFFSET %s
                    """,
                    (org_id, state_filter, limit, offset)
                )
            else:
                cursor.execute(
                    """
                    SELECT id, alert_id, alert_title, alert_state, search_name,
                           search_query, result_count, severity, payload, received_at, created_at
                    FROM splunk_alerts
                    WHERE org_id = %s
                    ORDER BY received_at DESC
                    LIMIT %s OFFSET %s
                    """,
                    (org_id, limit, offset)
                )

            alerts = cursor.fetchall()

            # Get total count
            if state_filter:
                cursor.execute(
                    "SELECT COUNT(*) FROM splunk_alerts WHERE org_id = %s AND alert_state = %s",
                    (org_id, state_filter)
                )
            else:
                cursor.execute(
                    "SELECT COUNT(*) FROM splunk_alerts WHERE org_id = %s",
                    (org_id,)
                )
            total_count = cursor.fetchone()[0]

        return jsonify({
            "alerts": [
                {
                    "id": row[0],
                    "alertId": row[1],
                    "title": row[2],
                    "state": row[3],
                    "searchName": row[4],
                    "searchQuery": row[5],
                    "resultCount": row[6],
                    "severity": row[7],
                    "payload": row[8],
                    "receivedAt": row[9].isoformat() if row[9] else None,
                    "createdAt": row[10].isoformat() if row[10] else None,
                }
                for row in alerts
            ],
            "total": total_count,
            "limit": limit,
            "offset": offset,
        })
    except Exception as exc:
        logger.exception("[SPLUNK] Failed to fetch alerts for user %s: %s", sanitize(user_id), sanitize(exc))
        return jsonify({"error": "Failed to fetch alerts"}), 500


@splunk_bp.route("/alerts/webhook-url", methods=["GET"])
@require_permission("connectors", "read")
def get_webhook_url(user_id):
    """Get the webhook URL that should be configured in Splunk."""
    base_url = external_backend_url()

    webhook_url = f"{base_url}/splunk/alerts/webhook/{user_id}"

    return jsonify({
        "webhookUrl": webhook_url,
        "instructions": [
            "1. Go to your Splunk instance",
            "2. Navigate to Settings -> Searches, reports, and alerts",
            "3. Find your saved search/alert and click Edit -> Edit Alert",
            "4. Under 'Trigger Actions', click 'Add Actions' -> 'Webhook'",
            "5. Paste the webhook URL above",
            "6. Save the alert configuration",
            "7. The alert will now send notifications to Aurora when triggered"
        ]
    })


@splunk_bp.route("/rca-settings", methods=["GET"])
@require_permission("connectors", "read")
def get_rca_settings(user_id):
    """Get Splunk RCA settings for the authenticated user."""
    rca_enabled = get_user_preference(user_id, "splunk_rca_enabled", default=False)

    return jsonify({
        "rcaEnabled": rca_enabled,
    })


@splunk_bp.route("/rca-settings", methods=["PUT"])
@require_permission("connectors", "write")
def update_rca_settings(user_id):
    """Update Splunk RCA settings for the authenticated user."""
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    rca_enabled = data.get("rcaEnabled", False)

    if not isinstance(rca_enabled, bool):
        return jsonify({"error": "rcaEnabled must be a boolean"}), 400

    store_user_preference(user_id, "splunk_rca_enabled", rca_enabled)
    logger.info(f"[SPLUNK] Updated RCA settings for user {sanitize(user_id)}: rcaEnabled={rca_enabled}")

    return jsonify({
        "success": True,
        "rcaEnabled": rca_enabled,
    })
