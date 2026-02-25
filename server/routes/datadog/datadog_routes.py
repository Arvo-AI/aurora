import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

import requests
from flask import Blueprint, jsonify, request

from routes.datadog.tasks import process_datadog_event
from utils.db.connection_pool import db_pool
from utils.web.cors_utils import create_cors_response
from utils.logging.secure_logging import mask_credential_value
from utils.auth.stateless_auth import get_user_id_from_request
from utils.auth.token_management import get_token_data, store_tokens_in_db
logger = logging.getLogger(__name__)

datadog_bp = Blueprint("datadog", __name__)

DATADOG_TIMEOUT = 20

_SITE_BASE_URLS: Dict[str, str] = {
    "datadoghq.com": "https://api.datadoghq.com",
    "us": "https://api.datadoghq.com",
    "us1": "https://api.datadoghq.com",
    "us1.datadoghq.com": "https://api.datadoghq.com",
    "datadoghq.eu": "https://api.datadoghq.eu",
    "eu": "https://api.datadoghq.eu",
    "us3": "https://api.us3.datadoghq.com",
    "us3.datadoghq.com": "https://api.us3.datadoghq.com",
    "us5": "https://api.us5.datadoghq.com",
    "us5.datadoghq.com": "https://api.us5.datadoghq.com",
    "ap1": "https://api.ap1.datadoghq.com",
    "ap1.datadoghq.com": "https://api.ap1.datadoghq.com",
    "ap2": "https://api.ap2.datadoghq.com",
    "ap2.datadoghq.com": "https://api.ap2.datadoghq.com",
    "gov": "https://api.ddog-gov.com",
    "ddog-gov.com": "https://api.ddog-gov.com",
}


class DatadogAPIError(Exception):
    """Custom error for Datadog API interactions."""


def _normalize_site(site: Optional[str]) -> Tuple[str, str]:
    """Normalize a user-supplied site string to a canonical site + base URL."""
    if not site:
        return "datadoghq.com", _SITE_BASE_URLS["datadoghq.com"]

    candidate = site.strip().lower()
    if not candidate:
        return "datadoghq.com", _SITE_BASE_URLS["datadoghq.com"]

    # Allow users to paste full URLs (https://api.datadoghq.com)
    if candidate.startswith("http://") or candidate.startswith("https://"):
        base_url = candidate.rstrip("/")
        return base_url.replace("https://", "").replace("http://", ""), base_url

    if candidate in _SITE_BASE_URLS:
        return candidate, _SITE_BASE_URLS[candidate]

    # Handle shorthand like "us5.datadoghq.com" → map to known host
    host_candidate = candidate.replace("https://", "").replace("http://", "")
    if host_candidate in _SITE_BASE_URLS:
        return host_candidate, _SITE_BASE_URLS[host_candidate]

    logger.warning("[DATADOG] Unknown site '%s', defaulting to datadoghq.com", site)
    return candidate, _SITE_BASE_URLS["datadoghq.com"]


def _to_rfc3339(ts: datetime) -> str:
    return ts.replace(microsecond=0).isoformat().replace("+00:00", "Z")


class DatadogClient:
    def __init__(self, api_key: str, app_key: str, site: Optional[str] = None):
        self.api_key = api_key
        self.app_key = app_key
        self.site, self.base_url = _normalize_site(site)

    @property
    def headers(self) -> Dict[str, str]:
        return {
            "DD-API-KEY": self.api_key,
            "DD-APPLICATION-KEY": self.app_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.base_url}{path}"
        try:
            response = requests.request(method, url, headers=self.headers, timeout=DATADOG_TIMEOUT, **kwargs)
        except requests.RequestException as exc:
            logger.error("[DATADOG] %s %s network error: %s", method, url, exc)
            raise DatadogAPIError("Unable to reach Datadog") from exc

        if response.status_code == 429:
            logger.warning("[DATADOG] Rate limited on %s %s", method, path)
            raise DatadogAPIError("Datadog API rate limit reached. Please retry later.")

        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            logger.error("[DATADOG] %s %s failed (%s): %s", method, url, response.status_code, response.text)
            raise DatadogAPIError(response.text or str(exc)) from exc

        return response

    def validate_credentials(self) -> Dict[str, Any]:
        return self._request("GET", "/api/v1/validate").json()

    def get_org(self) -> Optional[Dict[str, Any]]:
        try:
            return self._request("GET", "/api/v1/org").json()
        except DatadogAPIError:
            logger.debug("[DATADOG] Unable to fetch org metadata", exc_info=True)
            return None

    def search_logs(self, query: str, start: str, end: str, limit: int, cursor: Optional[str] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "filter": {
                "query": query or "*",
                "from": start,
                "to": end,
            },
            "page": {
                "limit": max(1, min(limit, 500)),
            },
            "sort": "desc",
        }
        if cursor:
            payload["page"]["cursor"] = cursor

        return self._request("POST", "/api/v2/logs/events/search", json=payload).json()

    def query_metrics(self, query: str, start_ms: int, end_ms: int, interval: Optional[int] = None) -> Dict[str, Any]:
        attributes: Dict[str, Any] = {
            "queries": [
                {
                    "data_source": "metrics",
                    "name": "query",
                    "query": query,
                }
            ],
            "from": start_ms,
            "to": end_ms,
        }
        if interval:
            attributes["interval"] = interval

        body = {
            "data": [
                {
                    "type": "timeseries_request",
                    "attributes": attributes,
                }
            ]
        }
        return self._request("POST", "/api/v2/query/timeseries", json=body).json()

    def list_events(self, start_ts: int, end_ts: int, params: Dict[str, Any]) -> Dict[str, Any]:
        query_params = {
            "start": start_ts,
            "end": end_ts,
        }
        if params.get("priority"):
            query_params["priority"] = params["priority"]
        if params.get("sources"):
            query_params["sources"] = params["sources"]
        if params.get("tags"):
            query_params["tags"] = params["tags"]

        return self._request("GET", "/api/v1/events", params=query_params).json()

    def list_monitors(self, params: Dict[str, Any]) -> Any:
        try:
            page = int(params.get("page", 0))
        except (TypeError, ValueError):
            page = 0

        try:
            page_size = int(params.get("page_size", 25))
        except (TypeError, ValueError):
            page_size = 25

        query_params = {
            "page": max(page, 0),
            "page_size": min(max(page_size, 1), 100),
            "group_states": params.get("group_states", "all"),
            "with_downtimes": str(params.get("with_downtimes", "true")).lower(),
        }
        if params.get("name"):
            query_params["name"] = params["name"]
        if params.get("tags"):
            query_params["monitor_tags"] = params["tags"]
        if params.get("status"):
            query_params["status"] = params["status"]

        return self._request("GET", "/api/v1/monitor", params=query_params).json()


def _get_stored_datadog_credentials(user_id: str) -> Optional[Dict[str, Any]]:
    try:
        data = get_token_data(user_id, "datadog")
        return data or None
    except Exception as exc:
        logger.error("[DATADOG] Failed to retrieve credentials for user %s: %s", user_id, exc)
        return None


def _build_client_from_creds(creds: Dict[str, Any]) -> Optional[DatadogClient]:
    api_key = creds.get("api_key")
    app_key = creds.get("app_key")
    site = creds.get("site")
    if not api_key or not app_key:
        return None
    return DatadogClient(api_key=api_key, app_key=app_key, site=site)


@datadog_bp.route("/connect", methods=["POST", "OPTIONS"])
def connect():
    if request.method == "OPTIONS":
        return create_cors_response()

    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}

    user_id = payload.get("userId") or get_user_id_from_request()
    api_key = payload.get("apiKey")
    app_key = payload.get("appKey")
    raw_site = payload.get("site")
    service_account = payload.get("serviceAccountName")

    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    if not api_key or not isinstance(api_key, str):
        return jsonify({"error": "Datadog API key is required"}), 400
    if not app_key or not isinstance(app_key, str):
        return jsonify({"error": "Datadog application key is required"}), 400

    site, base_url = _normalize_site(raw_site)

    masked_api = mask_credential_value(api_key)
    masked_app = mask_credential_value(app_key)
    logger.info("[DATADOG] Connecting user %s to site=%s api=%s app=%s", user_id, site, masked_api, masked_app)

    client = DatadogClient(api_key=api_key, app_key=app_key, site=site)

    try:
        validation = client.validate_credentials()
        if not validation.get("valid"):
            logger.warning("[DATADOG] Validation failed for user %s: %s", user_id, validation)
            return jsonify({"error": "Unable to validate Datadog credentials"}), 400
    except DatadogAPIError as exc:
        logger.error("[DATADOG] Credential validation failed for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to validate Datadog credentials"}), 502

    org_data = None
    try:
        org_data = client.get_org()
    except DatadogAPIError as exc:
        logger.debug("[DATADOG] Org lookup failed for user %s: %s", user_id, exc)

    token_payload = {
        "api_key": api_key,
        "app_key": app_key,
        "site": site,
        "base_url": base_url,
        "org_name": org_data.get("name") if org_data else None,
        "org_id": str(org_data.get("id")) if org_data and org_data.get("id") is not None else None,
        "validated_at": datetime.now(timezone.utc).isoformat(),
        "service_account_name": service_account,
    }

    try:
        store_tokens_in_db(user_id, token_payload, "datadog")
        logger.info("[DATADOG] Stored credentials for user %s (site=%s)", user_id, site)
    except Exception as exc:
        logger.exception("[DATADOG] Failed to store credentials: %s", exc)
        return jsonify({"error": "Failed to store Datadog credentials"}), 500

    response = {
        "success": True,
        "site": site,
        "baseUrl": base_url,
        "org": org_data,
        "serviceAccountName": service_account,
        "validated": True,
    }
    return jsonify(response)


@datadog_bp.route("/status", methods=["GET", "OPTIONS"])
def status():
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    creds = _get_stored_datadog_credentials(user_id)
    if not creds:
        return jsonify({"connected": False})

    client = _build_client_from_creds(creds)
    if not client:
        logger.warning("[DATADOG] Incomplete credentials for user %s", user_id)
        return jsonify({"connected": False})

    try:
        validation = client.validate_credentials()
        if not validation.get("valid"):
            return jsonify({"connected": False, "error": "Stored Datadog keys are no longer valid"})
    except DatadogAPIError as exc:
        logger.warning("[DATADOG] Status validation failed for user %s: %s", user_id, exc)
        return jsonify({"connected": False, "error": "Failed to validate stored Datadog credentials"})

    org_data = client.get_org()

    return jsonify({
        "connected": True,
        "site": creds.get("site"),
        "baseUrl": creds.get("base_url"),
        "org": org_data,
        "serviceAccountName": creds.get("service_account_name"),
        "validatedAt": creds.get("validated_at"),
    })


@datadog_bp.route("/disconnect", methods=["DELETE", "POST", "OPTIONS"])
def disconnect():
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
                (user_id, "datadog")
            )
            token_rows = cursor.rowcount
            cursor.execute(
                "DELETE FROM datadog_events WHERE user_id = %s",
                (user_id,)
            )
            event_rows = cursor.rowcount
            conn.commit()

        logger.info("[DATADOG] Disconnected user %s (tokens=%s, events=%s)", user_id, token_rows, event_rows)
        return jsonify({
            "success": True,
            "message": "Datadog disconnected successfully",
            "tokensDeleted": token_rows,
            "eventsDeleted": event_rows,
        })
    except Exception as exc:
        logger.exception("[DATADOG] Failed to disconnect user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to disconnect Datadog"}), 500


@datadog_bp.route("/logs/search", methods=["POST", "OPTIONS"])
def search_logs():
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    creds = _get_stored_datadog_credentials(user_id)
    if not creds:
        return jsonify({"error": "Datadog is not connected"}), 400

    client = _build_client_from_creds(creds)
    if not client:
        return jsonify({"error": "Stored Datadog credentials are incomplete"}), 400

    body = request.get_json(force=True, silent=True) or {}
    query = body.get("query", "*")
    limit = int(body.get("limit") or 50)
    cursor = body.get("cursor")

    now = datetime.now(timezone.utc)
    default_from = now - timedelta(minutes=15)

    start_str = body.get("from") or _to_rfc3339(default_from)
    end_str = body.get("to") or _to_rfc3339(now)

    try:
        data = client.search_logs(query=query, start=start_str, end=end_str, limit=limit, cursor=cursor)
        return jsonify(data)
    except DatadogAPIError as exc:
        logger.error("[DATADOG] Log search failed for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to search Datadog logs"}), 502


@datadog_bp.route("/metrics/query", methods=["POST", "OPTIONS"])
def query_metrics():
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    creds = _get_stored_datadog_credentials(user_id)
    if not creds:
        return jsonify({"error": "Datadog is not connected"}), 400

    client = _build_client_from_creds(creds)
    if not client:
        return jsonify({"error": "Stored Datadog credentials are incomplete"}), 400

    body = request.get_json(force=True, silent=True) or {}
    query = body.get("query")
    if not query:
        return jsonify({"error": "query is required"}), 400

    to_ms = int(body.get("toMs") or int(datetime.now(timezone.utc).timestamp() * 1000))
    from_ms = int(body.get("fromMs") or (to_ms - 15 * 60 * 1000))
    interval = body.get("interval")
    if interval is not None:
        try:
            interval = int(interval)
        except (ValueError, TypeError):
            interval = None

    try:
        data = client.query_metrics(query=query, start_ms=from_ms, end_ms=to_ms, interval=interval)
        return jsonify(data)
    except DatadogAPIError as exc:
        logger.error("[DATADOG] Metrics query failed for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to query Datadog metrics"}), 502


@datadog_bp.route("/events", methods=["GET", "OPTIONS"])
def list_events():
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    creds = _get_stored_datadog_credentials(user_id)
    if not creds:
        return jsonify({"error": "Datadog is not connected"}), 400

    client = _build_client_from_creds(creds)
    if not client:
        return jsonify({"error": "Stored Datadog credentials are incomplete"}), 400

    args = request.args
    try:
        end = int(args.get("end", int(datetime.now(timezone.utc).timestamp())))
    except ValueError:
        end = int(datetime.now(timezone.utc).timestamp())
    try:
        start = int(args.get("start", end - 3600))
    except ValueError:
        start = end - 3600

    params = {
        "priority": args.get("priority"),
        "sources": args.get("sources"),
        "tags": args.get("tags"),
    }

    try:
        data = client.list_events(start_ts=start, end_ts=end, params=params)
        return jsonify(data)
    except DatadogAPIError as exc:
        logger.error("[DATADOG] List events failed for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to list Datadog events"}), 502


@datadog_bp.route("/monitors", methods=["GET", "OPTIONS"])
def list_monitors():
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    creds = _get_stored_datadog_credentials(user_id)
    if not creds:
        return jsonify({"error": "Datadog is not connected"}), 400

    client = _build_client_from_creds(creds)
    if not client:
        return jsonify({"error": "Stored Datadog credentials are incomplete"}), 400

    raw_params = {
        "page": request.args.get("page"),
        "page_size": request.args.get("page_size"),
        "group_states": request.args.get("group_states"),
        "with_downtimes": request.args.get("with_downtimes"),
        "name": request.args.get("name"),
        "tags": request.args.get("tags"),
        "status": request.args.get("status"),
    }

    try:
        data = client.list_monitors(params=raw_params)
        return jsonify(data)
    except DatadogAPIError as exc:
        logger.error("[DATADOG] List monitors failed for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to list Datadog monitors"}), 502


@datadog_bp.route("/events/ingested", methods=["GET", "OPTIONS"])
def list_ingested_events():
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    limit = request.args.get("limit", default=50, type=int)
    offset = request.args.get("offset", default=0, type=int)
    status_filter = request.args.get("status")
    type_filter = request.args.get("event_type")

    try:
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()

            base_query = """
                SELECT id, event_type, event_title, status, scope, payload, received_at, created_at
                FROM datadog_events
                WHERE user_id = %s
            """
            params = [user_id]
            if status_filter:
                base_query += " AND status = %s"
                params.append(status_filter)
            if type_filter:
                base_query += " AND event_type = %s"
                params.append(type_filter)

            base_query += " ORDER BY received_at DESC LIMIT %s OFFSET %s"
            params.extend([limit, offset])

            cursor.execute(base_query, params)
            rows = cursor.fetchall()

            count_query = "SELECT COUNT(*) FROM datadog_events WHERE user_id = %s"
            count_params = [user_id]
            if status_filter:
                count_query += " AND status = %s"
                count_params.append(status_filter)
            if type_filter:
                count_query += " AND event_type = %s"
                count_params.append(type_filter)

            cursor.execute(count_query, count_params)
            total = cursor.fetchone()[0]

        events = []
        for row in rows:
            events.append({
                "id": row[0],
                "eventType": row[1],
                "title": row[2],
                "status": row[3],
                "scope": row[4],
                "payload": row[5],
                "receivedAt": row[6].isoformat() if row[6] else None,
                "createdAt": row[7].isoformat() if row[7] else None,
            })

        return jsonify({
            "events": events,
            "total": total,
            "limit": limit,
            "offset": offset,
        })
    except Exception as exc:
        logger.exception("[DATADOG] Failed to list ingested events for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to load Datadog webhook events"}), 500


@datadog_bp.route("/webhook/<user_id>", methods=["POST", "OPTIONS"])
def webhook(user_id: str):
    if request.method == "OPTIONS":
        return create_cors_response()

    if not user_id:
        logger.warning("[DATADOG] Webhook received without user_id")
        return jsonify({"error": "user_id is required"}), 400

    # Check if user has Datadog connected
    creds = get_token_data(user_id, "datadog")
    if not creds:
        logger.warning("[DATADOG] Webhook received for user %s with no Datadog connection", user_id)
        return jsonify({"error": "Datadog not connected for this user"}), 404

    payload_text = request.get_data(as_text=True) or ""
    # Webhook signature verification removed for OSS version

    payload = request.get_json(silent=True) or {}
    metadata = {
        "headers": dict(request.headers),
        "remote_addr": request.remote_addr,
    }
    logger.info("[DATADOG] Received webhook for user %s type=%s", user_id, payload.get("event_type"))

    process_datadog_event.delay(payload, metadata, user_id)
    return jsonify({"received": True})


@datadog_bp.route("/webhook-url", methods=["GET", "OPTIONS"])
def webhook_url():
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

    url = f"{base_url}/datadog/webhook/{user_id}"

    instructions = [
        "1. Navigate to Integrations → Webhooks in Datadog.",
        "2. Create a new webhook with the URL above.",
        "3. (Optional) Configure a custom payload to include monitor or event context.",
        "4. Add the webhook to your monitors or event rules.",
        "5. Save and test the webhook to verify connectivity.",
    ]

    return jsonify({
        "webhookUrl": url,
        "instructions": instructions,
    })
