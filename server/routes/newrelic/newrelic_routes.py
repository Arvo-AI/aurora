"""Flask routes for the New Relic connector.

Provides endpoints for:
- Connecting/disconnecting New Relic accounts (NerdGraph + optional license key)
- Credential validation against NerdGraph
- Webhook URL generation for alert notifications
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from flask import Blueprint, jsonify, request

from connectors.newrelic_connector.client import NewRelicClient, NewRelicAPIError
from utils.db.connection_pool import db_pool
from utils.logging.secure_logging import mask_credential_value
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request
from utils.secrets.secret_ref_utils import delete_user_secret

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

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT user_id FROM user_tokens WHERE org_id = %s AND provider = 'newrelic' AND is_active = TRUE AND secret_ref IS NOT NULL LIMIT 1",
                    (org_id,)
                )
                row = cursor.fetchone()

        if row:
            return get_token_data(row[0], "newrelic") or None

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
        return jsonify({"error": "Failed to validate New Relic credentials"}), 400

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
        return jsonify({"connected": False, "error": "Stored credentials are no longer valid. Please reconnect."})

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
    """Remove stored New Relic credentials and backing Vault secrets."""
    try:
        success, deleted = delete_user_secret(user_id, "newrelic")
        if not success:
            logger.warning("[NEWRELIC] Failed to clean up secrets during disconnect")
            return jsonify({"success": False, "error": "Failed to delete stored credentials"}), 500

        logger.info("[NEWRELIC] Disconnected user %s (deleted %d token rows)", user_id, deleted)
        return jsonify({
            "success": True,
            "message": "New Relic disconnected successfully",
            "tokensDeleted": deleted,
        })
    except Exception as exc:
        logger.exception("[NEWRELIC] Failed to disconnect user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to disconnect New Relic"}), 500


# ------------------------------------------------------------------
# Webhook URL (for UI setup instructions)
# ------------------------------------------------------------------


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
# Webhook receiver (called by New Relic — no RBAC, authenticates via user_id in URL)
# ------------------------------------------------------------------


@newrelic_bp.route("/webhook/<user_id>", methods=["POST", "OPTIONS"])
def webhook(user_id: str):
    """Receive alert notifications from New Relic and enqueue RCA processing."""
    if request.method == "OPTIONS":
        return jsonify({}), 200

    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    creds = _get_stored_newrelic_credentials(user_id)
    if not creds:
        logger.warning("[NEWRELIC] Webhook received for user %s with no connection", user_id)
        return jsonify({"error": "New Relic not connected for this user"}), 404

    payload = request.get_json(force=True, silent=True) or {}
    if not payload:
        return jsonify({"error": "Empty payload"}), 400

    metadata = {
        "headers": {k: v for k, v in request.headers if k.lower() not in ("authorization", "api-key")},
        "remote_addr": request.remote_addr,
    }

    issue_id = (
        payload.get("issueId")
        or payload.get("issue_id")
        or payload.get("incidentId")
        or payload.get("incident_id")
        or "unknown"
    )
    from routes.newrelic.tasks import extract_newrelic_title, process_newrelic_event
    title = extract_newrelic_title(payload)

    logger.info(
        "[NEWRELIC][WEBHOOK] Received alert for user %s: %s (issue=%s)",
        user_id, title, issue_id,
    )

    process_newrelic_event.delay(payload, metadata, user_id)

    return jsonify({"accepted": True, "issueId": str(issue_id)}), 202
