"""Elastic Cloud connector routes: connect/status/disconnect, alert webhook, RCA settings."""

import hmac
import json
import logging
import os
import re
import secrets
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from flask import Blueprint, jsonify, request

from connectors.elastic_connector.client import (
    ElasticAPIError,
    ElasticClient,
    normalize_api_key,
    normalize_index_pattern,
    normalize_url,
    parse_cloud_id,
)
from routes.elastic.tasks import process_elastic_alert
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import (
    get_org_id_from_request,
    get_user_preference,
    set_rls_context,
    store_user_preference,
)
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.db.connection_pool import db_pool
from utils.log_sanitizer import hash_for_log, sanitize
from utils.secrets.secret_ref_utils import delete_user_secret

logger = logging.getLogger(__name__)

elastic_bp = Blueprint("elastic", __name__)

WEBHOOK_HEADER_NAME = "X-Aurora-Webhook-Secret"
WEBHOOK_BASIC_USER = "aurora"
DEFAULT_INDEX_PATTERN = "logs-*"
RCA_PREFERENCE_KEY = "elastic_rca_enabled"

# Mustache body pasted into the Kibana Webhook connector action. Every key is
# read by routes/elastic/tasks.py; unknown variables render as "" in Kibana.
KIBANA_ACTION_BODY_TEMPLATE: Dict[str, str] = {
    "source": "kibana",
    "rule_id": "{{rule.id}}",
    "rule_name": "{{rule.name}}",
    "rule_type": "{{rule.type}}",
    "rule_tags": "{{rule.tags}}",
    "rule_url": "{{rule.url}}",
    "space_id": "{{rule.spaceId}}",
    "alert_id": "{{alert.id}}",
    "alert_uuid": "{{alert.uuid}}",
    "action_group": "{{alert.actionGroup}}",
    "action_group_name": "{{alert.actionGroupName}}",
    "flapping": "{{alert.flapping}}",
    "consecutive_matches": "{{alert.consecutiveMatches}}",
    "date": "{{date}}",
    "kibana_url": "{{kibanaBaseUrl}}",
    "reason": "{{context.reason}}",
    "value": "{{context.value}}",
    "threshold": "{{context.threshold}}",
    "group": "{{context.group}}",
    "timestamp": "{{context.timestamp}}",
    "title": "{{context.title}}",
    "message": "{{context.message}}",
    "view_in_app_url": "{{context.viewInAppUrl}}",
    "alert_details_url": "{{context.alertDetailsUrl}}",
    "hits": "{{context.hits}}",
}


def _arg(data: Dict[str, Any], *names: str, default: Any = None) -> Any:
    """Return the first present key (camelCase from the UI, snake_case from MCP)."""
    if not isinstance(data, dict):
        return default
    for name in names:
        if not isinstance(name, str):
            continue
        value = data.get(name)
        if value is not None:
            return value
    return default


def _get_stored_credentials(user_id: str) -> Optional[Dict[str, Any]]:
    try:
        return get_token_data(user_id, "elastic")
    except Exception:
        logger.exception("[ELASTIC] Failed to retrieve credentials for user %s", sanitize(user_id))
        return None


def _client_from_creds(creds: Dict[str, Any]) -> Optional[ElasticClient]:
    api_key = creds.get("api_key")
    es_url = creds.get("elasticsearch_url")
    if not api_key or not es_url:
        return None
    return ElasticClient(es_url, api_key, kibana_url=creds.get("kibana_url"))


_SERVERLESS_HOST_RE = re.compile(r"\.es\.[a-z0-9-]+\.(aws|gcp|azure)\.elastic\.cloud$", re.IGNORECASE)
_HOSTED_HOST_RE = re.compile(r"\.(found\.io|elastic-cloud\.com|cloud\.es\.io)$", re.IGNORECASE)


def _deployment_type(cloud_id: Optional[str], build_flavor: Optional[str], es_url: Optional[str] = None) -> str:
    """serverless | cloud_hosted | self_managed (build_flavor first, then URL heuristics)."""
    if (build_flavor or "").lower() == "serverless":
        return "serverless"
    if cloud_id:
        return "cloud_hosted"
    host = urlparse(es_url).hostname if es_url else None
    if host:
        if _SERVERLESS_HOST_RE.search(host):
            return "serverless"
        if _HOSTED_HOST_RE.search(host):
            return "cloud_hosted"
    return "self_managed"


def _validate_key(client: ElasticClient):
    """Validate an API key without requiring cluster privileges.

    ``GET /`` needs the cluster ``monitor`` privilege (even the built-in Viewer
    role lacks it), so ``_security/_authenticate`` is the required check and
    ``info()`` is best-effort. Returns ``(auth, info)``; raises ElasticAPIError
    when the key is unusable.
    """
    auth: Optional[Dict[str, Any]] = None
    info: Optional[Dict[str, Any]] = None
    auth_error: Optional[ElasticAPIError] = None
    try:
        auth = client.authenticate()
    except ElasticAPIError as exc:
        if exc.status_code in (403, 404):
            auth_error = exc  # endpoint unavailable/restricted → rely on info()
        else:
            raise
    try:
        info = client.info()
    except ElasticAPIError as exc:
        if exc.status_code == 403 and auth is not None:
            info = None  # key works but lacks 'monitor'; version/cluster stay unknown
        elif auth_error is not None or auth is None:
            raise auth_error or exc
        else:
            raise
    return auth, info


def _resolve_webhook_base_url() -> str:
    ngrok_url = os.getenv("NGROK_URL", "").rstrip("/")
    backend_url = os.getenv("NEXT_PUBLIC_BACKEND_URL", "").rstrip("/")
    if ngrok_url and backend_url.startswith("http://localhost"):
        return ngrok_url
    return backend_url


def _status_for_error(exc: ElasticAPIError) -> int:
    if exc.status_code in (400, 401, 403):
        return 400
    return 502


# --------------------------------------------------------------------------- #
# Connection lifecycle
# --------------------------------------------------------------------------- #


@elastic_bp.route("/connect", methods=["POST"])
@require_permission("connectors", "write")
def connect(user_id):
    """Validate an Elastic API key against Elasticsearch and store the connection."""
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    api_key = normalize_api_key(_arg(data, "apiKey", "api_key"))
    if not api_key:
        return jsonify({"error": "apiKey is required (paste the 'Encoded' value from Kibana → API keys)"}), 400

    cloud_id = _arg(data, "cloudId", "cloud_id")
    cloud_id = cloud_id.strip() if isinstance(cloud_id, str) else None
    raw_es_url = _arg(data, "elasticsearchUrl", "elasticsearch_url")
    raw_kibana_url = _arg(data, "kibanaUrl", "kibana_url")
    raw_index_pattern = _arg(data, "indexPattern", "index_pattern")
    if raw_index_pattern is not None and (not isinstance(raw_index_pattern, str) or len(raw_index_pattern) > 256):
        return jsonify({"error": "indexPattern is invalid"}), 400
    if raw_index_pattern and raw_index_pattern.strip():
        # Validate with the same rule the search paths apply, so a stored default
        # (e.g. "logs-*, filebeat-*" with a stray space) can never fail later.
        index_pattern = normalize_index_pattern(raw_index_pattern)
        if not index_pattern:
            return jsonify({
                "error": "indexPattern is invalid: use letters, digits, '_ . - *' and commas "
                         "(e.g. logs-*,filebeat-*)"
            }), 400
    else:
        index_pattern = DEFAULT_INDEX_PATTERN

    es_url: Optional[str] = None
    kibana_url: Optional[str] = None
    if cloud_id:
        try:
            es_url, kibana_url = parse_cloud_id(cloud_id)
        except ValueError as exc:
            logger.warning("[ELASTIC] Invalid Cloud ID from user %s: %s", sanitize(user_id), sanitize(exc))
            return jsonify({
                "error": "Invalid Cloud ID. Copy it from Elastic Cloud → Deployment → Manage → Cloud ID."
            }), 400
    else:
        es_url = normalize_url(raw_es_url)
        if not es_url:
            return jsonify({
                "error": "A Cloud ID or a valid Elasticsearch URL is required "
                         "(e.g. https://my-deployment.es.us-east-1.aws.elastic.cloud)"
            }), 400

    if raw_kibana_url:
        # Keep the path: self-managed Kibana is often served under server.basePath.
        explicit_kibana = normalize_url(raw_kibana_url, keep_path=True)
        if not explicit_kibana:
            return jsonify({"error": "kibanaUrl is not a valid URL"}), 400
        kibana_url = explicit_kibana

    logger.info(
        "[ELASTIC] Connecting user %s to %s (key_hash=%s, cloud_id=%s)",
        sanitize(user_id), sanitize(es_url), hash_for_log(api_key), bool(cloud_id),
    )

    client = ElasticClient(es_url, api_key, kibana_url=kibana_url)
    try:
        auth, info = _validate_key(client)
    except ElasticAPIError as exc:
        logger.warning("[ELASTIC] Connection validation failed for user %s: %s", sanitize(user_id), sanitize(exc))
        return jsonify({"error": f"Failed to validate Elastic credentials: {exc.message}"}), _status_for_error(exc)

    version_info = (info or {}).get("version") or {}
    version = version_info.get("number")
    build_flavor = version_info.get("build_flavor")
    cluster_name = (info or {}).get("cluster_name")
    username = (auth or {}).get("username") if isinstance(auth, dict) else None
    deployment_type = _deployment_type(cloud_id, build_flavor, es_url)

    kibana_info = client.kibana_status() if kibana_url else None
    kibana_reachable = kibana_info is not None

    existing = _get_stored_credentials(user_id) or {}
    webhook_secret = existing.get("webhook_secret") or secrets.token_urlsafe(32)

    from datetime import datetime, timezone

    token_payload = {
        "api_key": api_key,
        "elasticsearch_url": es_url,
        "kibana_url": kibana_url,
        "cloud_id": cloud_id,
        "deployment_type": deployment_type,
        "cluster_name": cluster_name,
        "version": version,
        "username": username,
        "index_pattern": index_pattern,
        "kibana_reachable": kibana_reachable,
        "webhook_secret": webhook_secret,
        "validated_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        store_tokens_in_db(user_id, token_payload, "elastic")
        logger.info(
            "[ELASTIC] Stored credentials for user %s (cluster=%s, type=%s, kibana=%s)",
            sanitize(user_id), sanitize(cluster_name), deployment_type, kibana_reachable,
        )
    except Exception:
        logger.exception("[ELASTIC] Failed to store credentials for user %s", sanitize(user_id))
        return jsonify({"error": "Failed to store Elastic credentials"}), 500

    return jsonify({
        "success": True,
        "connected": True,
        "deploymentType": deployment_type,
        "clusterName": cluster_name,
        "version": version,
        "elasticsearchUrl": es_url,
        "kibanaUrl": kibana_url,
        "kibanaReachable": kibana_reachable,
        "indexPattern": index_pattern,
        "username": username,
        "hasWebhookSecret": True,
    })


@elastic_bp.route("/status", methods=["GET"])
@require_permission("connectors", "read")
def status(user_id):
    """Live connection status (``GET /`` against Elasticsearch)."""
    creds = _get_stored_credentials(user_id)
    if not creds:
        return jsonify({"connected": False})

    client = _client_from_creds(creds)
    if not client:
        logger.warning("[ELASTIC] Incomplete credentials for user %s", sanitize(user_id))
        return jsonify({"connected": False})

    try:
        _auth, info = _validate_key(client)
    except ElasticAPIError as exc:
        logger.warning("[ELASTIC] Status check failed for user %s: %s", sanitize(user_id), sanitize(exc))
        return jsonify({"connected": False, "error": "Failed to validate stored Elastic credentials"})

    version_info = (info or {}).get("version") or {}
    return jsonify({
        "connected": True,
        "deploymentType": creds.get("deployment_type") or _deployment_type(
            creds.get("cloud_id"), version_info.get("build_flavor"), creds.get("elasticsearch_url")
        ),
        "clusterName": (info or {}).get("cluster_name") or creds.get("cluster_name"),
        "version": version_info.get("number") or creds.get("version"),
        "elasticsearchUrl": creds.get("elasticsearch_url"),
        "kibanaUrl": creds.get("kibana_url"),
        "kibanaReachable": bool(creds.get("kibana_reachable")),
        "indexPattern": creds.get("index_pattern") or DEFAULT_INDEX_PATTERN,
        "username": creds.get("username"),
        "hasWebhookSecret": bool(creds.get("webhook_secret")),
    })


@elastic_bp.route("/disconnect", methods=["POST", "DELETE"])
@require_permission("connectors", "write")
def disconnect(user_id):
    """Remove the stored Elastic connection."""
    try:
        success, deleted_count = delete_user_secret(user_id, "elastic")
        if not success:
            logger.warning("[ELASTIC] Failed to clean up secrets during disconnect")
            return jsonify({"success": False, "error": "Failed to delete stored credentials"}), 500
        logger.info("[ELASTIC] Disconnected provider")
        return jsonify({"success": True, "message": "Elastic disconnected successfully", "deleted": deleted_count})
    except Exception:
        logger.exception("[ELASTIC] Failed to disconnect provider")
        return jsonify({"error": "Failed to disconnect Elastic"}), 500


# --------------------------------------------------------------------------- #
# Alert webhook (Kibana Webhook connector → Aurora)
# --------------------------------------------------------------------------- #


def _extract_presented_secret() -> Optional[str]:
    """Read the webhook secret from Bearer, custom header, or Basic-auth password."""
    header_secret = request.headers.get(WEBHOOK_HEADER_NAME)
    if header_secret:
        return header_secret.strip()
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    if request.authorization and request.authorization.type == "basic":
        return request.authorization.password or None
    return None


def _webhook_secret_matches(stored: str, presented: Optional[str]) -> bool:
    if not stored or not presented:
        return False
    return hmac.compare_digest(stored.encode("utf-8"), presented.encode("utf-8"))


@elastic_bp.route("/alerts/webhook/<user_id>", methods=["POST"])
def alert_webhook(user_id: str):
    """Receive a Kibana alert action for a specific user.

    Authenticated with the per-connection webhook secret (constant-time
    compare) presented as HTTP Basic password, Bearer token, or the
    ``X-Aurora-Webhook-Secret`` header. Requests without a valid secret are
    rejected with 401 before any processing occurs.
    """
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    creds = _get_stored_credentials(user_id)
    if not creds:
        logger.warning("[ELASTIC] Webhook received for user %s with no Elastic connection", sanitize(user_id))
        return jsonify({"error": "Elastic not connected for this user"}), 404

    stored_secret = creds.get("webhook_secret")
    if not stored_secret:
        logger.warning("[ELASTIC] Webhook for user %s rejected — no webhook secret stored", sanitize(user_id))
        return jsonify({"error": "Webhook secret not configured. Reconnect Elastic to generate one."}), 401

    if not _webhook_secret_matches(stored_secret, _extract_presented_secret()):
        logger.warning("[ELASTIC] Invalid webhook secret for user %s", sanitize(user_id))
        return jsonify({"error": "Invalid webhook secret"}), 401

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raw = request.get_data(as_text=True) or ""
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            payload = {"raw": raw[:5000]}
    if not isinstance(payload, dict):
        payload = {"raw": payload}

    logger.info(
        "[ELASTIC] Received alert webhook for user %s: rule=%s group=%s",
        sanitize(user_id), sanitize(payload.get("rule_name", "unknown")), sanitize(payload.get("action_group", "")),
    )

    sensitive_headers = {"authorization", "cookie", "set-cookie", "proxy-authorization", "x-api-key", "x-csrf-token"}
    sanitized_headers = {}
    for key, value in request.headers.items():
        key_lower = key.lower()
        if key_lower in sensitive_headers or "token" in key_lower or "secret" in key_lower:
            sanitized_headers[key] = "<REDACTED>"
        else:
            sanitized_headers[key] = value

    metadata = {"headers": sanitized_headers, "remote_addr": request.remote_addr}
    process_elastic_alert.delay(payload, metadata, user_id)
    return jsonify({"received": True})


@elastic_bp.route("/alerts", methods=["GET"])
@require_permission("connectors", "read")
def get_alerts(user_id):
    """List ingested Kibana alerts for the caller's org."""
    org_id = get_org_id_from_request()
    limit = max(1, min(request.args.get("limit", 50, type=int), 500))
    offset = max(0, request.args.get("offset", 0, type=int))
    state_filter = request.args.get("state")

    try:
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()
            set_rls_context(cursor, conn, user_id, log_prefix="[ELASTIC]")
            where = "WHERE org_id = %s"
            params: list = [org_id]
            if state_filter:
                where += " AND alert_state = %s"
                params.append(state_filter)
            cursor.execute(
                f"""
                SELECT id, alert_id, alert_uuid, alert_title, alert_state, rule_id, rule_name,
                       rule_type, action_group, reason, severity, view_in_app_url, payload,
                       received_at, created_at
                FROM elastic_alerts
                {where}
                ORDER BY received_at DESC
                LIMIT %s OFFSET %s
                """,
                (*params, limit, offset),
            )
            rows = cursor.fetchall()
            cursor.execute(f"SELECT COUNT(*) FROM elastic_alerts {where}", tuple(params))
            total = cursor.fetchone()[0]

        return jsonify({
            "alerts": [
                {
                    "id": row[0],
                    "alertId": row[1],
                    "alertUuid": row[2],
                    "title": row[3],
                    "state": row[4],
                    "ruleId": row[5],
                    "ruleName": row[6],
                    "ruleType": row[7],
                    "actionGroup": row[8],
                    "reason": row[9],
                    "severity": row[10],
                    "viewInAppUrl": row[11],
                    "payload": row[12],
                    # Columns are naive TIMESTAMP holding UTC; append "Z" so the
                    # browser does not parse them as local time.
                    "receivedAt": (row[13].isoformat() + "Z") if row[13] else None,
                    "createdAt": (row[14].isoformat() + "Z") if row[14] else None,
                }
                for row in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        })
    except Exception as exc:
        logger.exception("[ELASTIC] Failed to fetch alerts for user %s: %s", sanitize(user_id), sanitize(exc))
        return jsonify({"error": "Failed to fetch alerts"}), 500


@elastic_bp.route("/alerts/webhook-url", methods=["GET"])
@require_permission("connectors", "write")
def get_webhook_url(user_id):
    """Webhook URL + secret + Kibana action body template.

    Requires ``connectors:write`` because the response reveals the secret.
    """
    creds = _get_stored_credentials(user_id)
    if not creds:
        return jsonify({"error": "Elastic not connected"}), 404

    webhook_secret = creds.get("webhook_secret")
    base_url = _resolve_webhook_base_url()
    webhook_url = f"{base_url}/elastic/alerts/webhook/{user_id}"

    return jsonify({
        "webhookUrl": webhook_url,
        "webhookSecret": webhook_secret,
        "headerName": WEBHOOK_HEADER_NAME,
        "basicAuthUsername": WEBHOOK_BASIC_USER,
        "actionBodyTemplate": json.dumps(KIBANA_ACTION_BODY_TEMPLATE, indent=2),
        "instructions": [
            "1. In Kibana go to Stack Management → Connectors → Create connector → Webhook.",
            "2. Name it 'Aurora', method POST, URL = the webhook URL above.",
            f"3. Authentication: choose Basic, username '{WEBHOOK_BASIC_USER}', password = the webhook secret "
            f"(or add a header '{WEBHOOK_HEADER_NAME}' with the secret as its value).",
            "4. Save the connector, then open the rule you want Aurora to investigate (Observability → Alerts → Manage rules).",
            "5. Add an action using the Aurora connector, set 'Run when' to the alert action group and action frequency to 'On status changes', and paste the action body template below into the Body.",
            "6. Add a second action row with the same connector and body, with 'Run when' = 'Recovered', so Aurora can close the loop.",
            "7. Save the rule. Turn on 'Enable Alert RCA' in Aurora to create incidents from these alerts.",
        ],
    })


# --------------------------------------------------------------------------- #
# RCA settings (default OFF)
# --------------------------------------------------------------------------- #


@elastic_bp.route("/rca-settings", methods=["GET"])
@require_permission("connectors", "read")
def get_rca_settings(user_id):
    rca_enabled = get_user_preference(user_id, RCA_PREFERENCE_KEY, default=False)
    return jsonify({"rcaEnabled": bool(rca_enabled)})


@elastic_bp.route("/rca-settings", methods=["PUT"])
@require_permission("connectors", "write")
def update_rca_settings(user_id):
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}
    rca_enabled = _arg(data, "rcaEnabled", "rca_enabled", default=False)
    if not isinstance(rca_enabled, bool):
        return jsonify({"error": "rcaEnabled must be a boolean"}), 400
    store_user_preference(user_id, RCA_PREFERENCE_KEY, rca_enabled)
    logger.info("[ELASTIC] Updated RCA settings for user %s: rcaEnabled=%s", sanitize(user_id), "true" if rca_enabled else "false")
    return jsonify({"success": True, "rcaEnabled": rca_enabled})
