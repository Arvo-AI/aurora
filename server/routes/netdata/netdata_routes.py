"""Netdata integration routes."""

import logging
import os
from typing import Any, Dict, Optional

from flask import Blueprint, jsonify, request

from routes.netdata.tasks import process_netdata_alert
from utils.db.connection_pool import db_pool
from utils.web.cors_utils import create_cors_response
from utils.auth.stateless_auth import get_user_id_from_request
from utils.auth.token_management import get_token_data, store_tokens_in_db

logger = logging.getLogger(__name__)

netdata_bp = Blueprint("netdata", __name__)


def _get_stored_netdata_credentials(user_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve stored Netdata credentials for user."""
    try:
        return get_token_data(user_id, "netdata")
    except Exception as exc:
        logger.error(f"Failed to retrieve Netdata credentials for user {user_id}: {exc}")
        return None


@netdata_bp.route("/connect", methods=["POST", "OPTIONS"])
def connect():
    """Store Netdata API token and space info."""
    if request.method == "OPTIONS":
        return create_cors_response()

    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    # Always use authenticated user ID - never trust userId from request body
    user_id = get_user_id_from_request()
    api_token = data.get("apiToken") or data.get("token")
    space_url = data.get("spaceUrl") or data.get("baseUrl")
    space_name = data.get("spaceName")

    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    if not api_token or not isinstance(api_token, str):
        return jsonify({"error": "Netdata API token is required"}), 400

    # Default to Netdata Cloud base URL
    base_url = space_url.rstrip("/") if space_url else "https://app.netdata.cloud"

    token_payload = {
        "api_token": api_token,
        "base_url": base_url,
        "space_name": space_name,
    }

    try:
        store_tokens_in_db(user_id, token_payload, "netdata")
        logger.info(f"[NETDATA] Stored credentials for user {user_id}")
    except Exception as exc:
        logger.exception(f"[NETDATA] Failed to store credentials for user {user_id}: {exc}")
        return jsonify({"error": "Failed to store Netdata credentials"}), 500

    return jsonify({
        "success": True,
        "baseUrl": base_url,
        "spaceName": space_name,
    })


@netdata_bp.route("/status", methods=["GET", "OPTIONS"])
def status():
    """Check Netdata connection status."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    logger.info(f"[NETDATA] Status check for user: {user_id}")
    
    if not user_id:
        logger.warning("[NETDATA] Status check: No user ID found")
        return jsonify({"error": "User authentication required"}), 401

    creds = _get_stored_netdata_credentials(user_id)
    logger.info(f"[NETDATA] Retrieved credentials: {bool(creds)}")
    
    if not creds:
        logger.info(f"[NETDATA] No credentials found for user {user_id}")
        return jsonify({"connected": False})

    api_token = creds.get("api_token")
    if not api_token:
        logger.info(f"[NETDATA] No api_token in credentials for user {user_id}")
        return jsonify({"connected": False})

    logger.info(f"[NETDATA] Status check successful for user {user_id}")
    return jsonify({
        "connected": True,
        "baseUrl": creds.get("base_url"),
        "spaceName": creds.get("space_name"),
    })


@netdata_bp.route("/disconnect", methods=["POST", "DELETE", "OPTIONS"])
def disconnect():
    """Disconnect Netdata by removing stored credentials."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    try:
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM user_tokens WHERE user_id = %s AND provider = %s",
                (user_id, "netdata")
            )
            token_rows = cursor.rowcount
            cursor.execute(
                "DELETE FROM netdata_alerts WHERE user_id = %s",
                (user_id,)
            )
            alert_rows = cursor.rowcount
            cursor.execute(
                "DELETE FROM netdata_verification_tokens WHERE user_id = %s",
                (user_id,)
            )
            conn.commit()

        logger.info(f"[NETDATA] Disconnected user {user_id} (tokens={token_rows}, alerts={alert_rows})")
        return jsonify({
            "success": True,
            "message": "Netdata disconnected successfully",
        })
    except Exception as exc:
        logger.exception(f"[NETDATA] Failed to disconnect user {user_id}: {exc}")
        return jsonify({"error": "Failed to disconnect Netdata"}), 500


@netdata_bp.route("/alerts/webhook/<user_id>", methods=["POST", "OPTIONS"])
def alert_webhook(user_id: str):
    """Receive alert webhook from Netdata."""
    if request.method == "OPTIONS":
        return create_cors_response()

    if not user_id:
        logger.warning("[NETDATA] Webhook received without user_id")
        return jsonify({"error": "user_id is required"}), 400

    # Check if user has Netdata connected
    creds = get_token_data(user_id, "netdata")
    if not creds:
        logger.warning("[NETDATA] Webhook received for user %s with no Netdata connection", user_id)
        return jsonify({"error": "Netdata not connected for this user"}), 404

    payload = request.get_json(silent=True) or {}
    # Netdata v2 API structure has alert.name nested under "alert" key
    alert_obj = payload.get("alert") or {}
    alert_name = (payload.get("alarm") or 
                  payload.get("title") or 
                  payload.get("alert_name") or
                  alert_obj.get("name") or 
                  "unknown")
                  
    logger.info("[NETDATA] Received webhook for user %s: %s", user_id, alert_name)
    logger.debug("[NETDATA] Payload keys: %s", list(payload.keys()) if payload else "empty")
    
    # Store verification token from test notifications directly in DB (not a secret)
    # Netdata sends it as "token" in test notifications
    token = payload.get("token")
    if token:
        logger.debug("[NETDATA] Extracted token: %s...", token[:20])
        try:
            with db_pool.get_admin_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO netdata_verification_tokens (user_id, token, created_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (user_id) DO UPDATE SET token = EXCLUDED.token, created_at = NOW()
                    """,
                    (user_id, token)
                )
                conn.commit()
            logger.info("[NETDATA] Stored verification token in DB")
        except Exception as e:
            logger.error(f"[NETDATA] Failed to store verification token: {e}")

    metadata = {"headers": dict(request.headers), "remote_addr": request.remote_addr}
    process_netdata_alert.delay(payload, metadata, user_id)
    return jsonify({"received": True})


@netdata_bp.route("/alerts", methods=["GET", "OPTIONS"])
def get_alerts():
    """Fetch stored Netdata alerts for user."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    limit = request.args.get("limit", 50, type=int)
    offset = request.args.get("offset", 0, type=int)
    status_filter = request.args.get("status")

    try:
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()

            if status_filter:
                cursor.execute(
                    """
                    SELECT id, alert_name, alert_status, chart, host, space, room, 
                           value, message, payload, received_at, created_at
                    FROM netdata_alerts
                    WHERE user_id = %s AND alert_status = %s
                    ORDER BY received_at DESC
                    LIMIT %s OFFSET %s
                    """,
                    (user_id, status_filter, limit, offset)
                )
            else:
                cursor.execute(
                    """
                    SELECT id, alert_name, alert_status, chart, host, space, room,
                           value, message, payload, received_at, created_at
                    FROM netdata_alerts
                    WHERE user_id = %s
                    ORDER BY received_at DESC
                    LIMIT %s OFFSET %s
                    """,
                    (user_id, limit, offset)
                )

            alerts = cursor.fetchall()

            # Get total count
            if status_filter:
                cursor.execute(
                    "SELECT COUNT(*) FROM netdata_alerts WHERE user_id = %s AND alert_status = %s",
                    (user_id, status_filter)
                )
            else:
                cursor.execute(
                    "SELECT COUNT(*) FROM netdata_alerts WHERE user_id = %s",
                    (user_id,)
                )
            total_count = cursor.fetchone()[0]

        return jsonify({
            "alerts": [
                {
                    "id": row[0],
                    "alertName": row[1],
                    "status": row[2],
                    "chart": row[3],
                    "host": row[4],
                    "space": row[5],
                    "room": row[6],
                    "value": row[7],
                    "message": row[8],
                    "payload": row[9],
                    "receivedAt": row[10].isoformat() if row[10] else None,
                    "createdAt": row[11].isoformat() if row[11] else None,
                }
                for row in alerts
            ],
            "total": total_count,
            "limit": limit,
            "offset": offset,
        })
    except Exception as exc:
        logger.exception("[NETDATA] Failed to fetch alerts for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to fetch alerts"}), 500


@netdata_bp.route("/alerts/webhook-url", methods=["GET", "OPTIONS"])
def get_webhook_url():
    """Get the webhook URL and verification token for Netdata configuration."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    # Use ngrok URL for development if available, otherwise use backend URL
    ngrok_url = os.getenv("NGROK_URL", "").rstrip("/")
    backend_url = os.getenv("NEXT_PUBLIC_BACKEND_URL", "").rstrip("/")

    # For development, prefer ngrok URL if available
    if ngrok_url and backend_url.startswith("http://localhost"):
        base_url = ngrok_url
    else:
        base_url = backend_url

    webhook_url = f"{base_url}/netdata/alerts/webhook/{user_id}"
    
    # Get verification token from DB
    verification_token = None
    try:
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT token FROM netdata_verification_tokens WHERE user_id = %s",
                (user_id,)
            )
            result = cursor.fetchone()
            if result:
                verification_token = result[0]
    except Exception as e:
        logger.warning("[NETDATA] Failed to fetch verification token: %s", e)

    return jsonify({
        "webhookUrl": webhook_url,
        "verificationToken": verification_token,
    })
