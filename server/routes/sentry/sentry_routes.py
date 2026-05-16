"""Flask routes for the Sentry connector.

Provides endpoints for:
- Connecting/disconnecting Sentry orgs via Internal Integration auth tokens
- Credential validation against the Sentry web API
- Webhook URL generation for integration setup
- Read-only proxies for issue/project listings
- Webhook ingestion with HMAC-SHA256 signature verification
"""

import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from flask import Blueprint, jsonify, request

from connectors.sentry_connector.client import SentryClient, SentryAPIError
from utils.db.connection_pool import db_pool
from utils.log_sanitizer import sanitize
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request, set_rls_context
from utils.secrets.secret_ref_utils import delete_user_secret
from routes.sentry.tasks import extract_sentry_title, process_sentry_event

logger = logging.getLogger(__name__)

sentry_bp = Blueprint("sentry", __name__)

SENTRY_TIMEOUT = 30

VALID_REGIONS = ("us", "eu")


def _get_stored_sentry_credentials(user_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve stored Sentry credentials for a user (or their org)."""
    try:
        data = get_token_data(user_id, "sentry")
        if data:
            return data

        org_id = get_org_id_from_request()
        if not org_id:
            return None

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix="[SENTRY:get_creds]")
                cursor.execute(
                    "SELECT user_id FROM user_tokens WHERE org_id = %s AND provider = 'sentry' AND is_active = TRUE AND secret_ref IS NOT NULL LIMIT 1",
                    (org_id,)
                )
                row = cursor.fetchone()

        if row:
            return get_token_data(row[0], "sentry") or None

        return None
    except Exception as exc:
        logger.error("[SENTRY] Failed to retrieve credentials for user %s: %s", user_id, exc)
        return None


def _build_client_from_creds(creds: Dict[str, Any]) -> Optional[SentryClient]:
    """Build a SentryClient from stored credential dict."""
    auth_token = creds.get("auth_token")
    org_slug = creds.get("org_slug")
    region = creds.get("region", "us")
    if not auth_token or not org_slug:
        return None
    try:
        return SentryClient(
            auth_token=auth_token,
            org_slug=org_slug,
            region=region,
            timeout=SENTRY_TIMEOUT,
        )
    except ValueError:
        return None


def _resolve_webhook_base_url() -> str:
    """Return the public base URL for receiving Sentry webhooks."""
    ngrok_url = os.getenv("NGROK_URL", "").rstrip("/")
    backend_url = os.getenv("NEXT_PUBLIC_BACKEND_URL", "").rstrip("/")
    if ngrok_url and backend_url.startswith("http://localhost"):
        return ngrok_url
    return backend_url


def _verify_sentry_signature(raw_body: bytes, signature_header: str, client_secret: str) -> bool:
    """Verify a Sentry webhook signature against the integration client_secret.

    Sentry signs every Integration Platform webhook with HMAC-SHA256 of the
    raw JSON body using the integration's client_secret. The hex digest is
    sent in the ``Sentry-Hook-Signature`` header (no scheme prefix).

    Constant-time comparison prevents timing attacks.
    """
    if not signature_header or not client_secret or not raw_body:
        return False
    try:
        digest = hmac.new(
            client_secret.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
    except Exception:
        return False
    return hmac.compare_digest(digest, signature_header.strip())


@sentry_bp.route("/connect", methods=["POST"])
@require_permission("connectors", "write")
def connect(user_id):
    """Store and validate Sentry Internal Integration credentials."""
    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}

    auth_token = payload.get("authToken") or payload.get("auth_token")
    org_slug = payload.get("orgSlug") or payload.get("org_slug")
    region = (payload.get("region") or "us").strip().lower()
    webhook_secret = payload.get("webhookSecret") or payload.get("webhook_secret") or ""

    if not auth_token or not isinstance(auth_token, str):
        return jsonify({"error": "Sentry auth token is required"}), 400
    if not org_slug or not isinstance(org_slug, str):
        return jsonify({"error": "Sentry organization slug is required"}), 400
    if region not in VALID_REGIONS:
        return jsonify({"error": "Region must be 'us' or 'eu'"}), 400

    org_slug = org_slug.strip()

    logger.info(
        "[SENTRY] Connecting user %s org=%s region=%s",
        sanitize(user_id), sanitize(org_slug), sanitize(region),
    )

    try:
        client = SentryClient(auth_token=auth_token, org_slug=org_slug, region=region)
    except ValueError as exc:
        logger.warning("[SENTRY] SentryClient construction failed: %s", sanitize(str(exc)))
        return jsonify({"error": "Invalid Sentry credentials format"}), 400

    try:
        org_info = client.validate_credentials()
    except SentryAPIError as exc:
        logger.warning("[SENTRY] Credential validation failed for user %s: status=%s", user_id, exc.status_code)
        if exc.status_code == 404:
            return jsonify({"error": f"Sentry organization '{org_slug}' not found or token has no access"}), 400
        if exc.status_code in (401, 403):
            return jsonify({"error": "Invalid Sentry auth token or insufficient permissions"}), 400
        return jsonify({"error": "Failed to validate Sentry credentials"}), 502

    accessible_projects = []
    try:
        projects = client.list_projects(limit=50)
        accessible_projects = [
            {"id": p.get("id"), "slug": p.get("slug"), "name": p.get("name"), "platform": p.get("platform")}
            for p in projects[:50]
        ]
    except SentryAPIError:
        logger.debug("[SENTRY] Could not list projects during connect", exc_info=True)

    token_payload = {
        "auth_token": auth_token,
        "org_slug": org_slug,
        "region": region,
        "client_secret": webhook_secret or None,
        "org_id_sentry": org_info.get("id"),
        "org_name": org_info.get("name"),
        "accessible_projects": accessible_projects,
        "validated_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        store_tokens_in_db(user_id, token_payload, "sentry")
        logger.info(
            "[SENTRY] Stored credentials for user %s (org=%s)",
            sanitize(user_id), sanitize(org_slug),
        )
    except Exception as exc:
        logger.exception("[SENTRY] Failed to store credentials: %s", exc)
        return jsonify({"error": "Failed to store Sentry credentials"}), 500

    return jsonify({
        "success": True,
        "region": region,
        "orgSlug": org_slug,
        "orgName": org_info.get("name"),
        "projectCount": len(accessible_projects),
        "accessibleProjects": accessible_projects[:10],
        "hasWebhookSecret": bool(webhook_secret),
        "validated": True,
    })


@sentry_bp.route("/status", methods=["GET"])
@require_permission("connectors", "read")
def status(user_id):
    """Check connection status by validating stored credentials."""
    creds = _get_stored_sentry_credentials(user_id)
    if not creds:
        return jsonify({"connected": False})

    client = _build_client_from_creds(creds)
    if not client:
        logger.warning("[SENTRY] Incomplete credentials for user %s", user_id)
        return jsonify({"connected": False})

    try:
        org_info = client.validate_credentials()
    except SentryAPIError as exc:
        logger.warning("[SENTRY] Status validation failed for user %s: status=%s", user_id, exc.status_code)
        return jsonify({
            "connected": False,
            "error": "Stored credentials are no longer valid. Please reconnect.",
        })

    return jsonify({
        "connected": True,
        "region": creds.get("region", "us"),
        "orgSlug": creds.get("org_slug"),
        "orgName": org_info.get("name") or creds.get("org_name"),
        "validatedAt": creds.get("validated_at"),
        "hasWebhookSecret": bool(creds.get("client_secret")),
        "accessibleProjects": creds.get("accessible_projects", []),
    })


@sentry_bp.route("/disconnect", methods=["DELETE", "POST"])
@require_permission("connectors", "write")
def disconnect(user_id):
    """Remove stored Sentry credentials and backing Vault secrets."""
    try:
        success, deleted = delete_user_secret(user_id, "sentry")
        if not success:
            logger.warning("[SENTRY] Failed to clean up secrets during disconnect")
            return jsonify({"success": False, "error": "Failed to delete stored credentials"}), 500

        logger.info(
            "[SENTRY] Disconnected user %s (deleted %d token rows)",
            sanitize(user_id), deleted,
        )
        return jsonify({
            "success": True,
            "message": "Sentry disconnected successfully",
            "tokensDeleted": deleted,
        })
    except Exception as exc:
        logger.exception("[SENTRY] Failed to disconnect user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to disconnect Sentry"}), 500


@sentry_bp.route("/webhook-url", methods=["GET"])
@require_permission("connectors", "read")
def webhook_url(user_id):
    """Return the webhook URL to configure in the Sentry Internal Integration."""
    base_url = _resolve_webhook_base_url()
    url = f"{base_url}/sentry/webhook/{user_id}"

    instructions = [
        "1. In Sentry, go to Settings → Custom Integrations → Create New Integration → Internal Integration.",
        "2. Name it 'Aurora' and paste the webhook URL above into the Webhook URL field.",
        "3. Under Permissions, grant read access to: Issue & Event, Project, Organization.",
        "4. Under Webhooks, subscribe to: issue and error (Business/Enterprise plans).",
        "5. Save the integration, then copy the Client Secret from the Credentials section.",
        "6. Under Tokens, click 'Create New Token' (Sentry does not auto-create one). Copy the sntrys_ token immediately — it is shown once.",
        "7. Paste the auth token and client secret into Aurora's connection form to complete setup.",
    ]

    return jsonify({
        "webhookUrl": url,
        "instructions": instructions,
    })


@sentry_bp.route("/projects", methods=["GET"])
@require_permission("connectors", "read")
def list_projects(user_id):
    """List Sentry projects accessible to the stored auth token."""
    creds = _get_stored_sentry_credentials(user_id)
    if not creds:
        return jsonify({"error": "Sentry not connected"}), 404

    client = _build_client_from_creds(creds)
    if not client:
        return jsonify({"error": "Incomplete Sentry credentials"}), 400

    try:
        projects = client.list_projects(limit=100)
        return jsonify({
            "projects": [
                {
                    "id": p.get("id"),
                    "slug": p.get("slug"),
                    "name": p.get("name"),
                    "platform": p.get("platform"),
                    "isMember": p.get("isMember"),
                }
                for p in projects
            ],
            "count": len(projects),
        })
    except SentryAPIError as exc:
        logger.warning("[SENTRY] list_projects failed for user %s: status=%s", user_id, exc.status_code)
        return jsonify({"error": "Failed to fetch projects from Sentry"}), 502
    except Exception as exc:
        logger.exception("[SENTRY] Unexpected error in list_projects for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to load Sentry projects"}), 500


@sentry_bp.route("/issues", methods=["GET"])
@require_permission("connectors", "read")
def list_issues(user_id):
    """Fetch issues from Sentry."""
    creds = _get_stored_sentry_credentials(user_id)
    if not creds:
        return jsonify({"error": "Sentry not connected"}), 404

    client = _build_client_from_creds(creds)
    if not client:
        return jsonify({"error": "Incomplete Sentry credentials"}), 400

    query = request.args.get("query", "is:unresolved")
    stats_period = request.args.get("statsPeriod", "24h")
    project_param = request.args.get("project")
    environment = request.args.get("environment")
    limit = request.args.get("limit", default=25, type=int)
    cursor = request.args.get("cursor")

    project_filter = [p.strip() for p in project_param.split(",") if p.strip()] if project_param else None

    try:
        data = client.list_issues(
            query=query,
            stats_period=stats_period,
            project=project_filter,
            environment=environment,
            limit=min(limit, 100),
            cursor=cursor,
        )
        return jsonify(data)
    except SentryAPIError as exc:
        logger.warning("[SENTRY] list_issues failed for user %s: status=%s", user_id, exc.status_code)
        return jsonify({"error": "Failed to fetch issues from Sentry"}), 502
    except Exception as exc:
        logger.exception("[SENTRY] Unexpected error in list_issues for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to load Sentry issues"}), 500


@sentry_bp.route("/events/ingested", methods=["GET"])
@require_permission("connectors", "read")
def list_ingested_events(user_id):
    """List Sentry webhook events stored in the database."""
    org_id = get_org_id_from_request()
    raw_limit = request.args.get("limit", default=50, type=int) or 50
    raw_offset = request.args.get("offset", default=0, type=int) or 0
    limit = max(1, min(raw_limit, 200))
    offset = max(0, raw_offset)
    resource_filter = request.args.get("resource")

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix="[Sentry]")

                base_query = """
                    SELECT id, issue_id, issue_title, level, project_slug, resource, action,
                           payload, received_at, created_at
                    FROM sentry_events
                    WHERE org_id = %s
                """
                params = [org_id]
                if resource_filter:
                    base_query += " AND resource = %s"
                    params.append(resource_filter)

                base_query += " ORDER BY received_at DESC LIMIT %s OFFSET %s"
                params.extend([limit, offset])

                cursor.execute(base_query, params)
                rows = cursor.fetchall()

                count_query = "SELECT COUNT(*) FROM sentry_events WHERE org_id = %s"
                count_params = [org_id]
                if resource_filter:
                    count_query += " AND resource = %s"
                    count_params.append(resource_filter)

                cursor.execute(count_query, count_params)
                total = cursor.fetchone()[0]

        events = []
        for row in rows:
            events.append({
                "id": row[0],
                "issueId": row[1],
                "title": row[2],
                "level": row[3],
                "projectSlug": row[4],
                "resource": row[5],
                "action": row[6],
                "payload": row[7],
                "receivedAt": row[8].isoformat() if row[8] else None,
                "createdAt": row[9].isoformat() if row[9] else None,
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
    """Receive Sentry Integration Platform webhooks.

    HMAC-SHA256 signature verification against the stored client_secret is
    mandatory: requests without a valid Sentry-Hook-Signature header are
    rejected with 401 before any processing occurs.
    """
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    creds = _get_stored_sentry_credentials(user_id)
    if not creds:
        logger.warning("[SENTRY] Webhook received for user %s with no connection", sanitize(user_id))
        return jsonify({"error": "Sentry not connected for this user"}), 404

    raw_body = request.get_data(cache=False) or b""
    signature_header = request.headers.get("Sentry-Hook-Signature", "")
    resource = request.headers.get("Sentry-Hook-Resource", "")
    request_id = request.headers.get("Request-ID", "")

    client_secret = creds.get("client_secret")
    if not client_secret:
        logger.warning(
            "[SENTRY] Webhook signature cannot be verified for user %s — no client_secret stored. Reject.",
            sanitize(user_id),
        )
        return jsonify({"error": "Webhook signing secret not configured. Reconnect Sentry with the webhook secret."}), 401

    if not _verify_sentry_signature(raw_body, signature_header, client_secret):
        logger.warning(
            "[SENTRY] Invalid webhook signature for user %s (resource=%s request_id=%s)",
            sanitize(user_id), sanitize(resource), sanitize(request_id),
        )
        return jsonify({"error": "Invalid webhook signature"}), 401

    try:
        payload = json.loads(raw_body) if raw_body else {}
    except Exception:
        payload = {}

    if not payload:
        return jsonify({"error": "Empty payload"}), 400

    metadata = {
        "resource": resource,
        "request_id": request_id,
        "timestamp": request.headers.get("Sentry-Hook-Timestamp", ""),
        "remote_addr": request.remote_addr,
    }

    title = extract_sentry_title(payload, resource)

    logger.info(
        "[SENTRY][WEBHOOK] Received %s for user %s: %s (request_id=%s)",
        sanitize(resource or "event"), sanitize(user_id), sanitize(title), sanitize(request_id),
    )

    process_sentry_event.delay(payload, metadata, user_id)

    return jsonify({"accepted": True, "resource": resource}), 202
