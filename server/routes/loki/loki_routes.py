import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from flask import Blueprint, jsonify, request

from connectors.loki_connector.client import LokiClient, LokiAPIError
from routes.loki.tasks import process_loki_alert
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request
from utils.db.connection_pool import db_pool
from utils.logging.secure_logging import mask_credential_value
from utils.secrets.secret_ref_utils import delete_user_secret
from utils.web.cors_utils import create_cors_response

logger = logging.getLogger(__name__)

loki_bp = Blueprint("loki", __name__)


def _normalize_base_url(raw_url: str) -> Optional[str]:
    """Normalize and validate a Loki base URL.

    Accepts both ``http://`` and ``https://`` schemes (Loki is often
    deployed on an internal network behind plain HTTP).
    """
    if not raw_url:
        return None

    url = raw_url.strip()
    if not url:
        return None

    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url

    url = url.rstrip("/")

    if not re.match(r"^https?://[A-Za-z0-9._:-]+(\/.*)?$", url):
        return None

    return url


def _get_stored_loki_credentials(user_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve stored Loki credentials from Vault for the given user."""
    try:
        return get_token_data(user_id, "loki")
    except Exception as exc:
        logger.error(f"Failed to retrieve Loki credentials for user {user_id}: {exc}")
        return None


def _build_loki_client(creds: Dict[str, Any]) -> Optional[LokiClient]:
    """Build a LokiClient from stored Vault credentials.

    Returns ``None`` when the credentials are incomplete (missing base_url).
    """
    base_url = creds.get("base_url")
    if not base_url:
        return None
    return LokiClient(
        base_url=base_url,
        auth_type=creds.get("auth_type", "none"),
        token=creds.get("token"),
        username=creds.get("username"),
        password=creds.get("password"),
        tenant_id=creds.get("tenant_id"),
    )


@loki_bp.route("/connect", methods=["POST", "OPTIONS"])
@require_permission("connectors", "write")
def connect(user_id):
    """Validate Loki credentials and store them in Vault."""
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    # --- Validate base URL ---
    raw_base_url = data.get("baseUrl")
    base_url = _normalize_base_url(raw_base_url) if raw_base_url else None
    if not base_url:
        return jsonify({
            "error": "A valid Loki base URL is required (e.g., https://loki.example.com:3100)"
        }), 400

    # --- Validate auth type ---
    auth_type = (data.get("authType") or "none").strip().lower()
    if auth_type not in ("bearer", "basic", "none"):
        return jsonify({"error": "authType must be one of: bearer, basic, none"}), 400

    # --- Extract credentials based on auth type ---
    token = None
    username = None
    password = None

    if auth_type == "bearer":
        token = (data.get("token") or "").strip()
        if not token:
            return jsonify({"error": "API token is required for Bearer authentication"}), 400

    if auth_type == "basic":
        username = (data.get("username") or "").strip()
        password = (data.get("password") or "").strip()
        if not username or not password:
            return jsonify({
                "error": "Username and password are required for Basic authentication"
            }), 400

    tenant_id = (data.get("tenantId") or "").strip() or None

    # --- Validate connection ---
    logger.info(f"[LOKI] Connecting user {user_id} to {base_url} (auth={auth_type})")
    if token:
        logger.info(f"[LOKI] Token: {mask_credential_value(token)}")

    client = LokiClient(
        base_url,
        auth_type=auth_type,
        token=token,
        username=username,
        password=password,
        tenant_id=tenant_id,
    )

    try:
        result = client.test_connection()
    except LokiAPIError as exc:
        logger.error(f"[LOKI] Connection validation failed for user {user_id}: {exc}")
        return jsonify({"error": f"Failed to validate Loki connection: {exc}"}), 502

    labels_count = len(result.get("labels", []))

    # --- Store credentials in Vault ---
    token_payload = {
        "auth_type": auth_type,
        "base_url": base_url,
        "tenant_id": tenant_id,
    }
    if auth_type == "bearer":
        token_payload["token"] = token
    elif auth_type == "basic":
        token_payload["username"] = username
        token_payload["password"] = password

    try:
        store_tokens_in_db(user_id, token_payload, "loki")
        logger.info(
            f"[LOKI] Stored credentials for user {user_id} "
            f"(auth={auth_type}, labels={labels_count})"
        )
    except Exception as exc:
        logger.exception(f"[LOKI] Failed to store credentials for user {user_id}: {exc}")
        return jsonify({"error": "Failed to store Loki credentials"}), 500

    return jsonify({
        "success": True,
        "baseUrl": base_url,
        "authType": auth_type,
        "tenantId": tenant_id,
        "labelsCount": labels_count,
    })


@loki_bp.route("/status", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def status(user_id):
    """Return Loki connection metadata for the authenticated user."""
    creds = _get_stored_loki_credentials(user_id)
    if not creds:
        return jsonify({"connected": False})

    base_url = creds.get("base_url")
    auth_type = creds.get("auth_type", "none")

    if not base_url:
        return jsonify({"connected": False})

    return jsonify({
        "connected": True,
        "baseUrl": base_url,
        "authType": auth_type,
        "tenantId": creds.get("tenant_id"),
    })


@loki_bp.route("/disconnect", methods=["POST", "DELETE", "OPTIONS"])
@require_permission("connectors", "write")
def disconnect(user_id):
    """Disconnect Loki by removing stored credentials from Vault."""
    try:
        success, deleted_count = delete_user_secret(user_id, "loki")
        if not success:
            logger.warning("[LOKI] Failed to clean up secrets during disconnect")
            return jsonify({
                "success": False,
                "error": "Failed to delete stored credentials"
            }), 500

        logger.info("[LOKI] Disconnected provider (deleted %s token entries)", deleted_count)

        return jsonify({
            "success": True,
            "message": "Loki disconnected successfully",
            "deleted": deleted_count,
        }), 200

    except Exception as exc:
        logger.exception("[LOKI] Failed to disconnect provider")
        return jsonify({"error": "Failed to disconnect Loki"}), 500


@loki_bp.route("/alerts/webhook/<user_id>", methods=["POST", "OPTIONS"])
def alert_webhook(user_id: str):
    """Receive alert webhook from Loki Ruler / Alertmanager."""
    if request.method == "OPTIONS":
        return create_cors_response()

    if not user_id:
        logger.warning("[LOKI] Webhook received without user_id")
        return jsonify({"error": "user_id is required"}), 400

    # Check if user has Loki connected
    creds = _get_stored_loki_credentials(user_id)
    if not creds:
        logger.warning(
            "[LOKI] Webhook received for user %s with no Loki connection", user_id
        )
        return jsonify({"error": "Loki not connected for this user"}), 404

    payload = request.get_json(silent=True) or {}

    # Log alert summary from top-level status
    alert_status = payload.get("status", "unknown")
    alerts_count = len(payload.get("alerts", []))
    logger.info(
        "[LOKI] Received webhook for user %s: status=%s alerts=%d",
        user_id,
        alert_status,
        alerts_count,
    )
    logger.debug(
        "[LOKI] Payload keys: %s", list(payload.keys()) if payload else "empty"
    )

    metadata = {"headers": dict(request.headers), "remote_addr": request.remote_addr}
    process_loki_alert.delay(payload, metadata, user_id)
    return jsonify({"received": True})


@loki_bp.route("/alerts", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def get_alerts(user_id):
    """Fetch stored Loki alerts for the authenticated user."""
    org_id = get_org_id_from_request()
    limit = request.args.get("limit", 50, type=int)
    offset = request.args.get("offset", 0, type=int)
    state_filter = request.args.get("state")

    try:
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SET myapp.current_org_id = %s", (org_id,))

            if state_filter:
                cursor.execute(
                    """
                    SELECT id, alert_uid, alert_title, alert_state, rule_group,
                           rule_name, labels, annotations, payload, received_at, created_at
                    FROM loki_alerts
                    WHERE org_id = %s AND alert_state = %s
                    ORDER BY received_at DESC
                    LIMIT %s OFFSET %s
                    """,
                    (org_id, state_filter, limit, offset),
                )
            else:
                cursor.execute(
                    """
                    SELECT id, alert_uid, alert_title, alert_state, rule_group,
                           rule_name, labels, annotations, payload, received_at, created_at
                    FROM loki_alerts
                    WHERE org_id = %s
                    ORDER BY received_at DESC
                    LIMIT %s OFFSET %s
                    """,
                    (org_id, limit, offset),
                )

            alerts = cursor.fetchall()

            if state_filter:
                cursor.execute(
                    "SELECT COUNT(*) FROM loki_alerts WHERE org_id = %s AND alert_state = %s",
                    (org_id, state_filter),
                )
            else:
                cursor.execute(
                    "SELECT COUNT(*) FROM loki_alerts WHERE org_id = %s",
                    (org_id,),
                )
            total_count = cursor.fetchone()[0]

        return jsonify({
            "alerts": [
                {
                    "id": row[0],
                    "alertUid": row[1],
                    "title": row[2],
                    "state": row[3],
                    "ruleGroup": row[4],
                    "ruleName": row[5],
                    "labels": row[6],
                    "annotations": row[7],
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
        logger.exception("[LOKI] Failed to fetch alerts: %s", exc)
        return jsonify({"error": "Failed to fetch alerts"}), 500


@loki_bp.route("/alerts/webhook-url", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def get_webhook_url(user_id):
    """Get the webhook URL that should be configured in Loki Ruler / Alertmanager."""
    ngrok_url = os.getenv("NGROK_URL", "").rstrip("/")
    backend_url = os.getenv("NEXT_PUBLIC_BACKEND_URL", "").rstrip("/")

    if ngrok_url and backend_url.startswith("http://localhost"):
        base_url = ngrok_url
    else:
        base_url = backend_url

    webhook_url = f"{base_url}/loki/alerts/webhook/{user_id}"

    return jsonify({
        "webhookUrl": webhook_url,
        "instructions": [
            "Option A: Configure Loki Ruler with Alertmanager",
            "1. Set alertmanager_url in your Loki ruler config to point to an Alertmanager instance",
            "2. In Alertmanager, add a webhook receiver with the URL above",
            "3. Example Alertmanager config:",
            "   receivers:",
            "     - name: aurora-webhook",
            "       webhook_configs:",
            "         - url: <webhook_url>",
            "",
            "Option B: Configure Grafana Alerting contact point",
            "1. In Grafana, go to Alerting > Contact points",
            "2. Add a new contact point with type 'Webhook'",
            "3. Paste the webhook URL above",
            "4. Route Loki-sourced alert rules to this contact point",
        ],
    })


@loki_bp.route("/logs/query_range", methods=["POST", "OPTIONS"])
@require_permission("connectors", "read")
def query_range(user_id):
    """Execute a LogQL range query over a time window.

    Also supports metric queries (rate, count_over_time) when the ``step``
    parameter is provided -- Loki returns matrix-type results instead of
    streams.
    """
    creds = _get_stored_loki_credentials(user_id)
    if not creds:
        return jsonify({"error": "Loki is not connected"}), 400

    client = _build_loki_client(creds)
    if not client:
        return jsonify({"error": "Stored Loki credentials are incomplete"}), 400

    body = request.get_json(force=True, silent=True) or {}

    query = body.get("query")
    if not query:
        return jsonify({"error": "query is required"}), 400

    limit = min(int(body.get("limit") or 100), 5000)
    direction = body.get("direction", "backward")

    # Default end = now, default start = 1 hour before end (nanosecond epoch)
    now_ns = str(int(datetime.now(timezone.utc).timestamp() * 1e9))
    end = body.get("end") or now_ns
    start = body.get("start") or str(int(end) - 3_600_000_000_000)

    step = body.get("step")  # Optional: triggers metric query when present

    try:
        data = client.query_range(
            query=query,
            start=start,
            end=end,
            limit=limit,
            direction=direction,
            step=step,
        )
        return jsonify(data)
    except LokiAPIError as exc:
        logger.error("[LOKI] Range query failed for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to query Loki logs"}), 502


@loki_bp.route("/logs/query", methods=["POST", "OPTIONS"])
@require_permission("connectors", "read")
def query_instant(user_id):
    """Execute a LogQL instant query at a single point in time."""
    creds = _get_stored_loki_credentials(user_id)
    if not creds:
        return jsonify({"error": "Loki is not connected"}), 400

    client = _build_loki_client(creds)
    if not client:
        return jsonify({"error": "Stored Loki credentials are incomplete"}), 400

    body = request.get_json(force=True, silent=True) or {}

    query = body.get("query")
    if not query:
        return jsonify({"error": "query is required"}), 400

    limit = min(int(body.get("limit") or 100), 5000)
    time_param = body.get("time")  # Optional point-in-time; Loki uses server time if absent
    direction = body.get("direction", "backward")

    try:
        data = client.query(
            query=query,
            limit=limit,
            time=time_param,
            direction=direction,
        )
        return jsonify(data)
    except LokiAPIError as exc:
        logger.error("[LOKI] Instant query failed for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to query Loki"}), 502


@loki_bp.route("/labels", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def list_labels(user_id):
    """List all known stream label names from the connected Loki instance."""
    creds = _get_stored_loki_credentials(user_id)
    if not creds:
        return jsonify({"error": "Loki is not connected"}), 400

    client = _build_loki_client(creds)
    if not client:
        return jsonify({"error": "Stored Loki credentials are incomplete"}), 400

    start = request.args.get("start")
    end = request.args.get("end")

    try:
        labels_list = client.labels(start=start, end=end)
        return jsonify({"status": "success", "data": labels_list})
    except LokiAPIError as exc:
        logger.error("[LOKI] Failed to fetch labels for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to fetch Loki labels"}), 502


@loki_bp.route("/label/<name>/values", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def label_values(user_id, name):
    """List known values for a specific stream label."""
    creds = _get_stored_loki_credentials(user_id)
    if not creds:
        return jsonify({"error": "Loki is not connected"}), 400

    client = _build_loki_client(creds)
    if not client:
        return jsonify({"error": "Stored Loki credentials are incomplete"}), 400

    start = request.args.get("start")
    end = request.args.get("end")

    try:
        values_list = client.label_values(label=name, start=start, end=end)
        return jsonify({"status": "success", "data": values_list})
    except LokiAPIError as exc:
        logger.error("[LOKI] Failed to fetch label values for user %s, label %s: %s", user_id, name, exc)
        return jsonify({"error": "Failed to fetch label values"}), 502
