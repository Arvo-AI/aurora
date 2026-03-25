"""Flask routes for the New Relic connector.

Provides endpoints for:
- Connecting/disconnecting New Relic accounts (NerdGraph + optional license key)
- Credential validation against NerdGraph
- Running NRQL queries
- Fetching issues and incidents
- Entity search
- Polling-based incident ingestion
- Webhook receiver for New Relic alert notifications
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from flask import Blueprint, jsonify, request

from connectors.newrelic_connector.client import NewRelicClient, NewRelicAPIError
from routes.newrelic.config import MAX_NRQL_LENGTH, MAX_RESULTS_CAP
from routes.newrelic.tasks import process_newrelic_issue, poll_newrelic_issues
from utils.db.connection_pool import db_pool
from utils.web.cors_utils import create_cors_response
from utils.logging.secure_logging import mask_credential_value
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request

logger = logging.getLogger(__name__)

newrelic_bp = Blueprint("newrelic", __name__)

NEWRELIC_TIMEOUT = 30


def _get_stored_newrelic_credentials(user_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve stored New Relic credentials for a user (or their org)."""
    try:
        data = get_token_data(user_id, "newrelic")
        if data:
            return data

        org_id = get_org_id_from_request()
        if not org_id:
            return None

        from utils.db.db_utils import connect_to_db_as_admin
        conn = connect_to_db_as_admin()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT user_id FROM user_tokens WHERE org_id = %s AND provider = 'newrelic' AND is_active = TRUE AND secret_ref IS NOT NULL LIMIT 1",
            (org_id,)
        )
        row = cursor.fetchone()
        cursor.close()
        conn.close()

        if row:
            data = get_token_data(row[0], "newrelic")
            return data or None

        return None
    except Exception as exc:
        logger.error("[NEWRELIC] Failed to retrieve credentials for user %s: %s", user_id, exc)
        return None


def _build_client_from_creds(creds: Dict[str, Any]) -> Optional[NewRelicClient]:
    """Build a NewRelicClient from stored credential dict."""
    api_key = creds.get("api_key")
    account_id = creds.get("account_id")
    region = creds.get("region", "us")
    if not api_key or not account_id:
        return None
    return NewRelicClient(
        api_key=api_key,
        account_id=str(account_id),
        region=region,
        timeout=NEWRELIC_TIMEOUT,
    )


# ------------------------------------------------------------------
# Connect / Status / Disconnect
# ------------------------------------------------------------------


@newrelic_bp.route("/connect", methods=["POST", "OPTIONS"])
@require_permission("connectors", "write")
def connect(user_id):
    """Store and validate New Relic credentials."""
    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}

    api_key = payload.get("apiKey")
    account_id = payload.get("accountId")
    region = payload.get("region", "us")
    license_key = payload.get("licenseKey")

    if not api_key or not isinstance(api_key, str):
        return jsonify({"error": "New Relic User API key is required"}), 400
    if not account_id:
        return jsonify({"error": "New Relic Account ID is required"}), 400

    account_id = str(account_id).strip()
    region = region.strip().lower() if region else "us"
    if region not in ("us", "eu"):
        return jsonify({"error": "Region must be 'us' or 'eu'"}), 400

    masked_key = mask_credential_value(api_key)
    logger.info(
        "[NEWRELIC] Connecting user %s account=%s region=%s key=%s",
        user_id, account_id, region, masked_key,
    )

    client = NewRelicClient(api_key=api_key, account_id=account_id, region=region)

    try:
        user_info = client.validate_credentials()
    except NewRelicAPIError as exc:
        logger.warning("[NEWRELIC] Credential validation failed for user %s: %s", user_id, exc)
        return jsonify({"error": f"Failed to validate New Relic credentials: {exc}"}), 400

    account_info = None
    try:
        account_info = client.get_account_info()
    except NewRelicAPIError as exc:
        logger.warning("[NEWRELIC] Account info lookup failed for user %s: %s", user_id, exc)

    accessible_accounts = []
    try:
        accessible_accounts = client.list_accessible_accounts()
    except NewRelicAPIError:
        logger.debug("[NEWRELIC] Could not list accessible accounts", exc_info=True)

    token_payload = {
        "api_key": api_key,
        "account_id": account_id,
        "region": region,
        "license_key": license_key,
        "user_email": user_info.get("email"),
        "user_name": user_info.get("name"),
        "account_name": account_info.get("name") if account_info else None,
        "accessible_accounts": [
            {"id": a["id"], "name": a["name"]} for a in accessible_accounts[:20]
        ],
        "validated_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        store_tokens_in_db(user_id, token_payload, "newrelic")
        logger.info("[NEWRELIC] Stored credentials for user %s (account=%s)", user_id, account_id)
    except Exception as exc:
        logger.exception("[NEWRELIC] Failed to store credentials: %s", exc)
        return jsonify({"error": "Failed to store New Relic credentials"}), 500

    return jsonify({
        "success": True,
        "region": region,
        "accountId": account_id,
        "accountName": account_info.get("name") if account_info else None,
        "userEmail": user_info.get("email"),
        "userName": user_info.get("name"),
        "accessibleAccounts": token_payload["accessible_accounts"],
        "validated": True,
    })


@newrelic_bp.route("/status", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def status(user_id):
    """Check connection status by validating stored credentials."""
    creds = _get_stored_newrelic_credentials(user_id)
    if not creds:
        return jsonify({"connected": False})

    client = _build_client_from_creds(creds)
    if not client:
        logger.warning("[NEWRELIC] Incomplete credentials for user %s", user_id)
        return jsonify({"connected": False})

    try:
        user_info = client.validate_credentials()
    except NewRelicAPIError as exc:
        logger.warning("[NEWRELIC] Status validation failed for user %s: %s", user_id, exc)
        return jsonify({"connected": False, "error": "Stored New Relic credentials are no longer valid"})

    return jsonify({
        "connected": True,
        "region": creds.get("region", "us"),
        "accountId": creds.get("account_id"),
        "accountName": creds.get("account_name"),
        "userEmail": user_info.get("email"),
        "userName": user_info.get("name"),
        "validatedAt": creds.get("validated_at"),
        "hasLicenseKey": bool(creds.get("license_key")),
        "accessibleAccounts": creds.get("accessible_accounts", []),
    })


@newrelic_bp.route("/disconnect", methods=["DELETE", "POST", "OPTIONS"])
@require_permission("connectors", "write")
def disconnect(user_id):
    """Remove stored New Relic credentials and associated events."""
    try:
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM user_tokens WHERE user_id = %s AND provider = %s",
                (user_id, "newrelic"),
            )
            token_rows = cursor.rowcount
            cursor.execute(
                "DELETE FROM newrelic_events WHERE user_id = %s",
                (user_id,),
            )
            event_rows = cursor.rowcount
            conn.commit()

        logger.info("[NEWRELIC] Disconnected user %s (tokens=%s, events=%s)", user_id, token_rows, event_rows)
        return jsonify({
            "success": True,
            "message": "New Relic disconnected successfully",
            "tokensDeleted": token_rows,
            "eventsDeleted": event_rows,
        })
    except Exception as exc:
        logger.exception("[NEWRELIC] Failed to disconnect user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to disconnect New Relic"}), 500


# ------------------------------------------------------------------
# NRQL Query Endpoint
# ------------------------------------------------------------------


@newrelic_bp.route("/nrql", methods=["POST", "OPTIONS"])
@require_permission("connectors", "read")
def nrql_query(user_id):
    """Execute an NRQL query via NerdGraph."""
    creds = _get_stored_newrelic_credentials(user_id)
    if not creds:
        return jsonify({"error": "New Relic is not connected"}), 400

    client = _build_client_from_creds(creds)
    if not client:
        return jsonify({"error": "Stored New Relic credentials are incomplete"}), 400

    body = request.get_json(force=True, silent=True) or {}
    nrql = body.get("query", "").strip()
    if not nrql:
        return jsonify({"error": "NRQL query is required"}), 400
    if len(nrql) > MAX_NRQL_LENGTH:
        return jsonify({"error": f"NRQL query exceeds maximum length of {MAX_NRQL_LENGTH} characters"}), 400

    override_account_id = body.get("accountId")
    timeout_seconds = min(int(body.get("timeout", 30)), 120)

    try:
        result = client.execute_nrql(
            nrql,
            account_id=override_account_id,
            timeout_seconds=timeout_seconds,
        )
        console_url = client.build_nrql_console_url(nrql)
        return jsonify({
            "results": result.get("results", []),
            "metadata": result.get("metadata"),
            "totalResult": result.get("totalResult"),
            "consoleUrl": console_url,
        })
    except NewRelicAPIError as exc:
        logger.error("[NEWRELIC] NRQL query failed for user %s: %s", user_id, exc)
        return jsonify({"error": f"NRQL query failed: {exc}"}), 502


# ------------------------------------------------------------------
# Issues & Incidents
# ------------------------------------------------------------------


@newrelic_bp.route("/issues", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def list_issues(user_id):
    """Fetch alert issues from NerdGraph with filtering support."""
    creds = _get_stored_newrelic_credentials(user_id)
    if not creds:
        return jsonify({"error": "New Relic is not connected"}), 400

    client = _build_client_from_creds(creds)
    if not client:
        return jsonify({"error": "Stored New Relic credentials are incomplete"}), 400

    args = request.args
    states = [s.upper() for s in args.get("states", "").split(",") if s.strip()] or None
    priorities = [p.upper() for p in args.get("priorities", "").split(",") if p.strip()] or None
    since_ms = args.get("sinceMs", type=int)
    until_ms = args.get("untilMs", type=int)
    cursor = args.get("cursor")
    page_size = min(int(args.get("pageSize", 25)), 100)

    try:
        result = client.get_issues(
            states=states,
            priorities=priorities,
            since_epoch_ms=since_ms,
            until_epoch_ms=until_ms,
            cursor=cursor,
            page_size=page_size,
        )
        return jsonify({
            "issues": result.get("issues", []),
            "nextCursor": result.get("nextCursor"),
        })
    except NewRelicAPIError as exc:
        logger.error("[NEWRELIC] Issues fetch failed for user %s: %s", user_id, exc)
        return jsonify({"error": f"Failed to fetch issues: {exc}"}), 502


@newrelic_bp.route("/issues/<issue_id>", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def get_issue(user_id, issue_id: str):
    """Get detailed info about a single issue."""
    creds = _get_stored_newrelic_credentials(user_id)
    if not creds:
        return jsonify({"error": "New Relic is not connected"}), 400

    client = _build_client_from_creds(creds)
    if not client:
        return jsonify({"error": "Stored New Relic credentials are incomplete"}), 400

    try:
        issue = client.get_issue_details(issue_id)
        if not issue:
            return jsonify({"error": f"Issue {issue_id} not found"}), 404
        return jsonify(issue)
    except NewRelicAPIError as exc:
        logger.error("[NEWRELIC] Issue detail fetch failed for user %s: %s", user_id, exc)
        return jsonify({"error": f"Failed to fetch issue details: {exc}"}), 502


# ------------------------------------------------------------------
# Entity search
# ------------------------------------------------------------------


@newrelic_bp.route("/entities", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def search_entities(user_id):
    """Search for New Relic entities (services, hosts, etc.)."""
    creds = _get_stored_newrelic_credentials(user_id)
    if not creds:
        return jsonify({"error": "New Relic is not connected"}), 400

    client = _build_client_from_creds(creds)
    if not client:
        return jsonify({"error": "Stored New Relic credentials are incomplete"}), 400

    query_str = request.args.get("query", "")
    entity_type = request.args.get("type")
    limit = min(int(request.args.get("limit", 25)), 200)

    try:
        entities = client.search_entities(
            query_str=query_str,
            entity_type=entity_type,
            limit=limit,
        )
        return jsonify({"entities": entities})
    except NewRelicAPIError as exc:
        logger.error("[NEWRELIC] Entity search failed for user %s: %s", user_id, exc)
        return jsonify({"error": f"Entity search failed: {exc}"}), 502


# ------------------------------------------------------------------
# Accounts listing
# ------------------------------------------------------------------


@newrelic_bp.route("/accounts", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def list_accounts(user_id):
    """List all New Relic accounts accessible by the stored API key."""
    creds = _get_stored_newrelic_credentials(user_id)
    if not creds:
        return jsonify({"error": "New Relic is not connected"}), 400

    client = _build_client_from_creds(creds)
    if not client:
        return jsonify({"error": "Stored New Relic credentials are incomplete"}), 400

    try:
        accounts = client.list_accessible_accounts()
        return jsonify({"accounts": accounts})
    except NewRelicAPIError as exc:
        logger.error("[NEWRELIC] Accounts list failed for user %s: %s", user_id, exc)
        return jsonify({"error": f"Failed to list accounts: {exc}"}), 502


# ------------------------------------------------------------------
# Ingested events (stored from webhooks / polling)
# ------------------------------------------------------------------


@newrelic_bp.route("/events/ingested", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def list_ingested_events(user_id):
    """List New Relic events stored from webhooks or polling."""
    org_id = get_org_id_from_request()
    limit = request.args.get("limit", default=50, type=int)
    offset = request.args.get("offset", default=0, type=int)
    status_filter = request.args.get("status")
    priority_filter = request.args.get("priority")

    try:
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SET myapp.current_org_id = %s", (org_id,))

            base_query = """
                SELECT id, issue_id, issue_title, priority, state, entity_names, payload, received_at, created_at
                FROM newrelic_events
                WHERE org_id = %s
            """
            params = [org_id]
            if status_filter:
                base_query += " AND state = %s"
                params.append(status_filter)
            if priority_filter:
                base_query += " AND priority = %s"
                params.append(priority_filter)

            base_query += " ORDER BY received_at DESC LIMIT %s OFFSET %s"
            params.extend([limit, offset])

            cursor.execute(base_query, params)
            rows = cursor.fetchall()

            count_query = "SELECT COUNT(*) FROM newrelic_events WHERE org_id = %s"
            count_params = [org_id]
            if status_filter:
                count_query += " AND state = %s"
                count_params.append(status_filter)
            if priority_filter:
                count_query += " AND priority = %s"
                count_params.append(priority_filter)

            cursor.execute(count_query, count_params)
            total = cursor.fetchone()[0]

        events = []
        for row in rows:
            events.append({
                "id": row[0],
                "issueId": row[1],
                "title": row[2],
                "priority": row[3],
                "state": row[4],
                "entityNames": row[5],
                "payload": row[6],
                "receivedAt": row[7].isoformat() if row[7] else None,
                "createdAt": row[8].isoformat() if row[8] else None,
            })

        return jsonify({
            "events": events,
            "total": total,
            "limit": limit,
            "offset": offset,
        })
    except Exception as exc:
        logger.exception("[NEWRELIC] Failed to list ingested events for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to load New Relic events"}), 500


# ------------------------------------------------------------------
# Webhook receiver for New Relic alert notifications
# ------------------------------------------------------------------


@newrelic_bp.route("/webhook/<user_id>", methods=["POST", "OPTIONS"])
def webhook(user_id: str):
    """Receive New Relic alert notification webhooks."""
    if request.method == "OPTIONS":
        return create_cors_response()

    if not user_id:
        logger.warning("[NEWRELIC] Webhook received without user_id")
        return jsonify({"error": "user_id is required"}), 400

    creds = get_token_data(user_id, "newrelic")
    if not creds:
        logger.warning("[NEWRELIC] Webhook received for user %s with no New Relic connection", user_id)
        return jsonify({"error": "New Relic not connected for this user"}), 404

    payload = request.get_json(silent=True) or {}
    metadata = {
        "headers": dict(request.headers),
        "remote_addr": request.remote_addr,
    }

    logger.info(
        "[NEWRELIC] Received webhook for user %s issue_id=%s",
        user_id, payload.get("issueId") or payload.get("issue_id"),
    )

    process_newrelic_issue.delay(payload, metadata, user_id)
    return jsonify({"received": True})


@newrelic_bp.route("/webhook-url", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def webhook_url(user_id):
    """Get the webhook URL to configure in New Relic."""
    ngrok_url = os.getenv("NGROK_URL", "").rstrip("/")
    backend_url = os.getenv("NEXT_PUBLIC_BACKEND_URL", "").rstrip("/")

    if ngrok_url and backend_url.startswith("http://localhost"):
        base_url = ngrok_url
    else:
        base_url = backend_url

    url = f"{base_url}/newrelic/webhook/{user_id}"

    instructions = [
        "1. In New Relic, navigate to Alerts → Destinations.",
        "2. Create a new Webhook destination with the URL above.",
        "3. Under Workflows, create or edit a workflow.",
        "4. Add a notification channel using the webhook destination.",
        "5. Configure the workflow filter for the issues you want Aurora to investigate.",
        "6. Save and test the webhook to verify connectivity.",
    ]

    return jsonify({
        "webhookUrl": url,
        "instructions": instructions,
    })


# ------------------------------------------------------------------
# Polling trigger (manual or cron-driven)
# ------------------------------------------------------------------


@newrelic_bp.route("/poll-issues", methods=["POST", "OPTIONS"])
@require_permission("connectors", "write")
def trigger_poll(user_id):
    """Manually trigger issue polling from New Relic."""
    creds = _get_stored_newrelic_credentials(user_id)
    if not creds:
        return jsonify({"error": "New Relic is not connected"}), 400

    task = poll_newrelic_issues.delay(user_id)
    return jsonify({"success": True, "taskId": task.id, "message": "Polling triggered"})
