"""Sentry connector routes and client."""

import hashlib
import hmac
import logging
import os
from typing import Any, Dict, List, Optional

import requests
from flask import Blueprint, jsonify, request

from routes.sentry.tasks import process_sentry_event
from utils.db.connection_pool import db_pool
from utils.log_sanitizer import sanitize, hash_for_log
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request, set_rls_context
from utils.secrets.secret_ref_utils import delete_user_secret

logger = logging.getLogger(__name__)

sentry_bp = Blueprint("sentry", __name__)


class SentryAPIError(Exception):
    """Custom error for Sentry API interactions."""


class SentryClient:
    def __init__(self, auth_token: str, base_url: str = "", organization: str = ""):
        self.auth_token = auth_token
        self.base_url = base_url.rstrip("/")
        self.organization = organization

    @property
    def headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.auth_token}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.base_url}{path}"
        try:
            response = requests.request(method, url, headers=self.headers, timeout=20, **kwargs)
        except requests.RequestException as exc:
            logger.error("[SENTRY] %s %s network error: %s", method, url, exc)
            raise SentryAPIError("Unable to reach Sentry") from exc

        if response.status_code == 429:
            logger.warning("[SENTRY] Rate limited on %s %s", method, path)
            raise SentryAPIError("Sentry API rate limit reached. Please retry later.")

        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            logger.error("[SENTRY] %s %s failed (%s): %s", method, url, response.status_code, response.text)
            raise SentryAPIError(response.text or str(exc)) from exc

        return response

    def list_projects(self) -> List[Dict[str, Any]]:
        return self._request("GET", "/api/0/projects/").json()

    def list_issues(self, query: str = "", start: str = "", end: str = "", limit: int = 100, project: str = "") -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"per_page": min(limit, 100)}
        if query:
            params["query"] = query
        if start:
            params["start"] = start
        if end:
            params["end"] = end

        if project and self.organization:
            path = f"/api/0/projects/{self.organization}/{project}/issues/"
        elif self.organization:
            path = f"/api/0/organizations/{self.organization}/issues/"
        else:
            path = "/api/0/issues/"

        return self._request("GET", path, params=params).json()


    def validate_credentials(self) -> Dict[str, Any]:
        if self.organization:
            return self._request("GET", f"/api/0/organizations/{self.organization}/").json()
        return self._request("GET", "/api/0/").json()


def _get_stored_sentry_credentials(user_id: str) -> Optional[Dict[str, Any]]:
    try:
        data = get_token_data(user_id, "sentry")
        if data:
            return data

        org_id = get_org_id_from_request()
        if not org_id:
            return None

        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()
            set_rls_context(cursor, conn, user_id, log_prefix="[Sentry:_get_stored_sentry_credentials]")
            cursor.execute(
                "SELECT user_id FROM user_tokens WHERE org_id = %s AND provider = 'sentry' AND is_active = TRUE AND secret_ref IS NOT NULL LIMIT 1",
                (org_id,)
            )
            row = cursor.fetchone()

        if row:
            data = get_token_data(row[0], "sentry")
            return data or None

        return None
    except Exception as exc:
        logger.error("[SENTRY] Failed to retrieve credentials for user %s: %s", user_id, exc)
        return None


def _build_client_from_creds(creds: Dict[str, Any]) -> Optional[SentryClient]:
    auth_token = creds.get("auth_token") or creds.get("authToken")
    if not auth_token:
        return None
    base_url = creds.get("base_url") or creds.get("baseUrl")
    organization = creds.get("org_slug") or creds.get("organization") or ""
    return SentryClient(auth_token=auth_token, base_url=base_url, organization=organization)


@sentry_bp.route("/connect", methods=["POST"])
@require_permission("connectors", "write")
def connect(user_id):
    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}

    auth_token = payload.get("authToken") or payload.get("auth_token")
    org_slug = payload.get("orgSlug") or payload.get("org_slug") or payload.get("organization") or ""
    base_url = payload.get("baseUrl") or payload.get("base_url")
    client_secret = payload.get("clientSecret") or payload.get("client_secret") or ""

    if not auth_token or not isinstance(auth_token, str):
        return jsonify({"error": "Sentry auth token is required"}), 400
    if not org_slug or not isinstance(org_slug, str):
        return jsonify({"error": "Sentry organization slug is required"}), 400

    base_url = base_url.rstrip("/")

    logger.info("[SENTRY] Connecting user %s to org=%s token_hash=%s", sanitize(user_id), sanitize(org_slug), hash_for_log(auth_token))

    client = SentryClient(auth_token=auth_token, base_url=base_url, organization=org_slug)

    try:
        org_data = client.validate_credentials()
    except SentryAPIError as exc:
        logger.warning("[SENTRY] Validation failed for user %s: %s", sanitize(user_id), exc)
        return jsonify({"error": "Unable to validate Sentry credentials. Check your token and organization slug."}), 400

    org_name = org_data.get("name") if isinstance(org_data, dict) else None

    token_payload = {
        "auth_token": auth_token,
        "org_slug": org_slug,
        "base_url": base_url,
        "org_name": org_name,
        "client_secret": client_secret,
    }

    try:
        store_tokens_in_db(user_id, token_payload, "sentry")
        logger.info("[SENTRY] Stored credentials for user %s (org=%s)", sanitize(user_id), sanitize(org_slug))
    except Exception as exc:
        logger.exception("[SENTRY] Failed to store credentials: %s", exc)
        return jsonify({"error": "Failed to store Sentry credentials"}), 500

    return jsonify({
        "success": True,
        "orgSlug": org_slug,
        "baseUrl": base_url,
        "orgName": org_name,
    })


@sentry_bp.route("/disconnect", methods=["DELETE", "POST"])
@require_permission("connectors", "write")
def disconnect(user_id):
    try:
        success, token_rows = delete_user_secret(user_id, "sentry")
        if not success:
            logger.warning("[SENTRY] Failed to clean up secrets during disconnect")

        event_rows = 0
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()
            set_rls_context(cursor, conn, user_id, log_prefix="[SENTRY:disconnect]")
            cursor.execute(
                "DELETE FROM sentry_events WHERE user_id = %s",
                (user_id,)
            )
            event_rows = cursor.rowcount
            conn.commit()

        logger.info("[SENTRY] Disconnected provider (tokens=%s, events=%s)", token_rows, event_rows)
        return jsonify({
            "success": True,
            "message": "Sentry disconnected successfully",
            "tokensDeleted": token_rows,
            "eventsDeleted": event_rows,
        })
    except Exception as exc:
        logger.exception("[SENTRY] Failed to disconnect provider")
        return jsonify({"error": "Failed to disconnect Sentry"}), 500


@sentry_bp.route("/status", methods=["GET"])
@require_permission("connectors", "read")
def status(user_id):
    creds = _get_stored_sentry_credentials(user_id)
    if not creds:
        return jsonify({"connected": False})

    client = _build_client_from_creds(creds)
    if not client:
        logger.warning("[SENTRY] Incomplete credentials for user %s", user_id)
        return jsonify({"connected": False})

    try:
        org_data = client.validate_credentials()
        org_name = org_data.get("name") if isinstance(org_data, dict) else None
        return jsonify({
            "connected": True,
            "orgSlug": creds.get("org_slug") or client.organization,
            "baseUrl": creds.get("base_url") or client.base_url,
            "orgName": org_name or creds.get("org_name"),
        })
    except SentryAPIError as exc:
        logger.warning("[SENTRY] Status validation failed for user %s: %s", user_id, exc)
        return jsonify({"connected": False, "error": "Stored Sentry credentials are no longer valid"})


@sentry_bp.route("/issues", methods=["GET"])
@require_permission("connectors", "read")
def list_issues(user_id):
    creds = _get_stored_sentry_credentials(user_id)
    if not creds:
        return jsonify({"error": "Sentry is not connected"}), 400

    client = _build_client_from_creds(creds)
    if not client:
        return jsonify({"error": "Stored Sentry credentials are incomplete"}), 400

    args = request.args
    query = args.get("query", "is:unresolved")
    limit = int(args.get("limit", 25))
    project = args.get("project", "")

    try:
        issues = client.list_issues(query=query, limit=limit, project=project)
        return jsonify({"issues": issues, "count": len(issues)})
    except SentryAPIError as exc:
        logger.error("[SENTRY] List issues failed for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to list Sentry issues"}), 502


@sentry_bp.route("/events/ingested", methods=["GET"])
@require_permission("connectors", "read")
def list_ingested_events(user_id):
    org_id = get_org_id_from_request()
    limit = request.args.get("limit", default=50, type=int)
    offset = request.args.get("offset", default=0, type=int)

    try:
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()
            set_rls_context(cursor, conn, user_id, log_prefix="[Sentry]")

            base_query = """
                SELECT id, event_type, event_title, status, scope, payload, received_at, created_at
                FROM sentry_events
                WHERE org_id = %s
            """
            params: list = [org_id]

            base_query += " ORDER BY received_at DESC LIMIT %s OFFSET %s"
            params.extend([limit, offset])

            cursor.execute(base_query, params)
            rows = cursor.fetchall()

            count_query = "SELECT COUNT(*) FROM sentry_events WHERE org_id = %s"
            cursor.execute(count_query, [org_id])
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
        logger.exception("[SENTRY] Failed to list ingested events for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to load Sentry webhook events"}), 500


@sentry_bp.route("/webhook/<user_id>", methods=["POST"])
def webhook(user_id: str):
    if not user_id:
        logger.warning("[SENTRY] Webhook received without user_id")
        return jsonify({"error": "user_id is required"}), 400

    creds = get_token_data(user_id, "sentry")
    if not creds:
        logger.warning("[SENTRY] Webhook received for user %s with no Sentry connection", sanitize(user_id))
        return jsonify({"error": "Sentry not connected for this user"}), 404

    client_secret = creds.get("client_secret") or ""
    if client_secret:
        signature = request.headers.get("Sentry-Hook-Signature", "")
        body = request.get_data()
        expected = hmac.HMAC(
            client_secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            logger.warning("[SENTRY] Invalid webhook signature for user %s", sanitize(user_id))
            return jsonify({"error": "Invalid signature"}), 401

    payload = request.get_json(silent=True) or {}
    metadata = {
        "content_type": request.headers.get("Content-Type"),
        "sentry_hook_resource": request.headers.get("Sentry-Hook-Resource"),
        "remote_addr": request.remote_addr,
    }
    logger.info("[SENTRY] Received webhook for user %s action=%s", sanitize(user_id), sanitize(payload.get("action")))

    process_sentry_event.delay(payload, metadata, user_id)
    return jsonify({"received": True})


@sentry_bp.route("/webhook-url", methods=["GET"])
@require_permission("connectors", "read")
def webhook_url(user_id):
    ngrok_url = os.getenv("NGROK_URL", "").rstrip("/")
    backend_url = os.getenv("NEXT_PUBLIC_BACKEND_URL", "").rstrip("/")

    if ngrok_url and backend_url.startswith("http://localhost"):
        base_url = ngrok_url
    else:
        base_url = backend_url

    if not base_url:
        return jsonify({"error": "NEXT_PUBLIC_BACKEND_URL is not configured. Cannot generate webhook URL."}), 500

    url = f"{base_url}/sentry/webhook/{user_id}"

    return jsonify({"webhookUrl": url})
