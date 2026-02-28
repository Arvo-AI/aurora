import json
import logging
import os
import re
from typing import Any
from urllib.parse import urlparse, urlunparse

import requests
from flask import Blueprint, jsonify, request

from routes.dynatrace.tasks import process_dynatrace_problem
from utils.db.connection_pool import db_pool
from utils.web.cors_utils import create_cors_response
from utils.auth.stateless_auth import (
    get_user_id_from_request,
    get_user_preference,
    store_user_preference,
)
from utils.auth.token_management import get_token_data, store_tokens_in_db
from routes.dynatrace.config import DYNATRACE_TIMEOUT

logger = logging.getLogger(__name__)

dynatrace_bp = Blueprint("dynatrace", __name__)


class DynatraceAPIError(Exception):
    """Custom error for Dynatrace API interactions."""


class DynatraceClient:
    """Client for interacting with the Dynatrace Environment API v2."""

    def __init__(self, environment_url: str, api_token: str):
        self.environment_url = environment_url
        self.headers = {
            "Authorization": f"Api-Token {api_token}",
            "Accept": "application/json",
        }

    @staticmethod
    def normalize_environment_url(raw_url: str) -> str | None:
        if not raw_url or not raw_url.strip():
            return None
        url = raw_url.strip()
        if not re.match(r"^https?://", url, re.IGNORECASE):
            url = "https://" + url
        parsed = urlparse(url)

        # Auto-convert .apps. to .live. — Dynatrace uses different domains for UI vs API
        netloc = parsed.netloc.replace(".apps.dynatrace.com", ".live.dynatrace.com")

        url = urlunparse((parsed.scheme, netloc, parsed.path.rstrip("/"), "", "", ""))
        if not re.match(r"^https?://[A-Za-z0-9._-]+(:[0-9]{2,5})?(/e/[A-Za-z0-9_-]+)?$", url):
            return None
        return url

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.environment_url}{path}"
        try:
            resp = requests.request(method, url, headers=self.headers, timeout=DYNATRACE_TIMEOUT, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.exceptions.Timeout as exc:
            logger.error("[DYNATRACE] %s %s timeout", method, url)
            raise DynatraceAPIError("Connection timed out. Check if Dynatrace is reachable.") from exc
        except requests.exceptions.ConnectionError as exc:
            logger.error("[DYNATRACE] %s %s connection error", method, url)
            raise DynatraceAPIError("Unable to connect. Verify the environment URL.") from exc
        except requests.HTTPError as exc:
            logger.error("[DYNATRACE] %s %s failed (%s): %s", method, url, resp.status_code, resp.text[:200])
            raise DynatraceAPIError(f"API error ({resp.status_code})") from exc
        except requests.RequestException as exc:
            logger.error("[DYNATRACE] %s %s error: %s", method, url, exc)
            raise DynatraceAPIError("Unable to reach Dynatrace") from exc

    def validate_connection(self) -> dict[str, Any]:
        # /api/v2/apiTokens/lookup validates the token itself and works regardless of
        # other scopes; /api/v1/time returns 406 on some tenants due to strict header enforcement
        api_token = self.headers["Authorization"].removeprefix("Api-Token ")
        return self._request("POST", "/api/v2/apiTokens/lookup", json={"token": api_token}).json()

    def get_cluster_version(self) -> str | None:
        try:
            return self._request("GET", "/api/v1/config/clusterversion").json().get("version")
        except DynatraceAPIError:
            return None


def _get_stored_credentials(user_id: str) -> dict[str, Any] | None:
    try:
        return get_token_data(user_id, "dynatrace")
    except Exception as exc:
        logger.error("Failed to retrieve Dynatrace credentials for user %s: %s", user_id, exc)
        return None


@dynatrace_bp.route("/connect", methods=["POST", "OPTIONS"])
def connect():
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    data = request.get_json(force=True, silent=True) or {}
    api_token = data.get("apiToken")
    raw_url = data.get("environmentUrl")

    if not api_token or not isinstance(api_token, str):
        return jsonify({"error": "apiToken is required"}), 400

    environment_url = DynatraceClient.normalize_environment_url(raw_url) if raw_url else None
    if not environment_url:
        return jsonify({"error": "A valid Dynatrace environment URL is required (e.g., https://abc12345.live.dynatrace.com)"}), 400

    logger.info("[DYNATRACE] Connecting user %s to %s", user_id, environment_url)

    client = DynatraceClient(environment_url, api_token)
    try:
        client.validate_connection()
    except DynatraceAPIError as exc:
        logger.error("[DYNATRACE] Connection validation failed for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to validate Dynatrace credentials"}), 502

    version = client.get_cluster_version()

    try:
        store_tokens_in_db(user_id, {"api_token": api_token, "environment_url": environment_url, "version": version}, "dynatrace")
    except Exception as exc:
        logger.exception("[DYNATRACE] Failed to store credentials for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to store Dynatrace credentials"}), 500

    return jsonify({"success": True, "environmentUrl": environment_url, "version": version})


@dynatrace_bp.route("/status", methods=["GET", "OPTIONS"])
def status():
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    creds = _get_stored_credentials(user_id)
    if not creds or not creds.get("api_token") or not creds.get("environment_url"):
        return jsonify({"connected": False})

    return jsonify({
        "connected": True,
        "environmentUrl": creds["environment_url"],
        "version": creds.get("version"),
    })


@dynatrace_bp.route("/disconnect", methods=["POST", "DELETE", "OPTIONS"])
def disconnect():
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    try:
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM user_tokens WHERE user_id = %s AND provider = %s", (user_id, "dynatrace"))
            conn.commit()
        return jsonify({"success": True, "message": "Dynatrace disconnected successfully"})
    except Exception as exc:
        logger.exception("[DYNATRACE] Failed to disconnect user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to disconnect Dynatrace"}), 500


@dynatrace_bp.route("/webhook/<user_id>", methods=["POST", "OPTIONS"])
def webhook(user_id: str):
    if request.method == "OPTIONS":
        return create_cors_response()

    creds = get_token_data(user_id, "dynatrace")
    if not creds:
        logger.warning("[DYNATRACE] Webhook received for user %s with no connection", user_id)
        return jsonify({"error": "Dynatrace not connected for this user"}), 404

    payload = request.get_json(silent=True) or {}
    logger.info("[DYNATRACE] Received webhook for user %s: %s", user_id, payload.get("ProblemTitle", "unknown"))

    _REDACTED_HEADERS = {"authorization", "cookie", "set-cookie", "proxy-authorization", "x-api-key"}
    sanitized_headers = {
        k: ("<REDACTED>" if k.lower() in _REDACTED_HEADERS or "token" in k.lower() or "secret" in k.lower() else v)
        for k, v in request.headers
    }

    process_dynatrace_problem.delay(payload, {"headers": sanitized_headers, "remote_addr": request.remote_addr}, user_id)
    return jsonify({"received": True})


@dynatrace_bp.route("/alerts", methods=["GET", "OPTIONS"])
def get_alerts():
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    limit = request.args.get("limit", 50, type=int)
    offset = request.args.get("offset", 0, type=int)
    state_filter = request.args.get("state")

    try:
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()

            conditions = ["user_id = %s"]
            params: list = [user_id]
            if state_filter:
                conditions.append("problem_state = %s")
                params.append(state_filter)
            where = "WHERE " + " AND ".join(conditions)

            cursor.execute(
                f"""SELECT id, problem_id, problem_title, problem_state, severity,
                           impact, impacted_entity, problem_url, tags, payload,
                           received_at, created_at, COUNT(*) OVER() AS total
                    FROM dynatrace_problems {where}
                    ORDER BY received_at DESC LIMIT %s OFFSET %s""",
                (*params, limit, offset),
            )
            rows = cursor.fetchall()

        total = rows[0][12] if rows else 0
        return jsonify({
            "alerts": [
                {
                    "id": r[0], "problemId": r[1], "title": r[2], "state": r[3],
                    "severity": r[4], "impact": r[5], "impactedEntity": r[6],
                    "problemUrl": r[7], "tags": r[8], "payload": r[9],
                    "receivedAt": r[10].isoformat() if r[10] else None,
                    "createdAt": r[11].isoformat() if r[11] else None,
                }
                for r in rows
            ],
            "total": total, "limit": limit, "offset": offset,
        })
    except Exception as exc:
        logger.exception("[DYNATRACE] Failed to fetch alerts for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to fetch alerts"}), 500


@dynatrace_bp.route("/webhook-url", methods=["GET", "OPTIONS"])
def get_webhook_url():
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    ngrok_url = os.getenv("NGROK_URL", "").rstrip("/")
    backend_url = os.getenv("NEXT_PUBLIC_BACKEND_URL", "").rstrip("/")
    base_url = ngrok_url if ngrok_url and backend_url.startswith("http://localhost") else backend_url

    return jsonify({
        "webhookUrl": f"{base_url}/dynatrace/webhook/{user_id}",
        "suggestedPayload": json.dumps({
            "ProblemID": "{ProblemID}",
            "PID": "{PID}",
            "State": "{State}",
            "ProblemTitle": "{ProblemTitle}",
            "ProblemSeverity": "{ProblemSeverity}",
            "ProblemImpact": "{ProblemImpact}",
            "ImpactedEntity": "{ImpactedEntity}",
            "ProblemURL": "{ProblemURL}",
            "Tags": "{Tags}",
        }, indent=2),
        "instructions": [
            "1. In Dynatrace, open the classic UI: https://<your-env-id>.live.dynatrace.com/ui/settings/integration/notifications",
            "2. Click 'Add notification' and select 'Custom Integration'",
            "3. Paste the webhook URL above into the 'Webhook URL' field",
            "4. Paste the suggested payload into the 'Custom payload' field",
            "5. Optionally assign an Alerting Profile to filter problems",
            "6. Click 'Send test notification' to verify, then save",
        ],
    })


@dynatrace_bp.route("/rca-settings", methods=["GET", "OPTIONS"])
def get_rca_settings():
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    return jsonify({"rcaEnabled": get_user_preference(user_id, "dynatrace_rca_enabled", default=False)})


@dynatrace_bp.route("/rca-settings", methods=["PUT", "OPTIONS"])
def update_rca_settings():
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    data = request.get_json(force=True, silent=True) or {}
    rca_enabled = data.get("rcaEnabled", False)
    if not isinstance(rca_enabled, bool):
        return jsonify({"error": "rcaEnabled must be a boolean"}), 400

    store_user_preference(user_id, "dynatrace_rca_enabled", rca_enabled)
    logger.info("[DYNATRACE] Updated RCA settings for user %s: rcaEnabled=%s", user_id, rca_enabled)
    return jsonify({"success": True, "rcaEnabled": rca_enabled})
