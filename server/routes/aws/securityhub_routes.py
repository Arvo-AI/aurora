"""
AWS Security Hub route handlers.
Provides webhooks for EventBridge and API endpoints for fetching processed findings.
"""
import logging
import uuid
import hmac
import os
from flask import Blueprint, request, jsonify
from prometheus_client import Counter, Histogram
from psycopg2.extras import RealDictCursor
from utils.db.connection_pool import db_pool
from .tasks import process_securityhub_finding
from utils.web.cors_utils import create_cors_response
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

securityhub_bp = Blueprint("securityhub", __name__)

EVENTBRIDGE_EVENTS_RECEIVED = Counter(
    "aws_securityhub_events_received_total", 
    "Total EventBridge Security Hub events received"
)
EVENTBRIDGE_EVENTS_FAILED = Counter(
    "aws_securityhub_events_failed_total", 
    "Total EventBridge Security Hub events failed",
    ["reason"]
)
EVENTBRIDGE_PROCESSING_LATENCY = Histogram(
    "aws_securityhub_processing_latency_seconds",
    "Processing time for Security Hub webhooks"
)

def _validate_api_key(org_id: str, api_key: str) -> bool:
    """Validate the incoming api key against what's configured for the org_id."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                # We check the user_tokens table for an aws_securityhub configuration
                # Org ID is mapped to a tenant config
                cursor.execute(
                    """
                    SELECT token_data FROM user_tokens 
                    WHERE org_id = %s AND provider = 'aws_securityhub' AND is_active = true
                    LIMIT 1
                    """,
                    (org_id,)
                )
                row = cursor.fetchone()
                if not row:
                    # For testing out-of-the-box in dev environments
                    dev_key = os.getenv("DEV_SECURITYHUB_API_KEY")
                    if os.getenv("FLASK_ENV") == "development" and dev_key and hmac.compare_digest(api_key, dev_key):
                        return True
                    return False
                
                token_data = row[0] or {}
                expected_key = token_data.get("api_key")
                if not expected_key:
                    return False
                return hmac.compare_digest(expected_key, api_key)
    except Exception as exc:
        logger.exception("[SECURITY_HUB] Failed to validate API key: %s", exc)
        return False

def _abort_webhook(reason: str, msg: str, status_code: int, org_id: str):
    """Helper to reduce code duplication for webhook validation failures."""
    EVENTBRIDGE_EVENTS_FAILED.labels(reason=reason).inc()
    logger.warning("[SECURITY_HUB] %s for org %s", msg, sanitize(org_id))
    return jsonify({"error": msg}), status_code

@securityhub_bp.route("/webhook/<org_id>", methods=["OPTIONS"])
def webhook_options(org_id: str):
    """Handle CORS preflight OPTIONS requests for the webhook endpoint."""
    return create_cors_response()

@securityhub_bp.route("/webhook/<org_id>", methods=["POST"])
@EVENTBRIDGE_PROCESSING_LATENCY.time()
def webhook(org_id: str):
    """
    Handle POST requests for AWS Security Hub EventBridge webhooks.
    Includes API key validation and enqueues background processing.
    """
    api_key = request.headers.get("x-api-key")
    if not api_key:
        return _abort_webhook("missing_api_key", "Missing x-api-key header", 401, org_id)

    if not _validate_api_key(org_id, api_key):
        return _abort_webhook("invalid_api_key", "Invalid API Key", 403, org_id)

    payload = request.get_json(silent=True)
    if not payload:
        return _abort_webhook("invalid_json", "Invalid JSON payload", 400, org_id)

    if payload.get("source") != "aws.securityhub":
        return _abort_webhook("invalid_source", "Invalid event source. Must be aws.securityhub", 400, org_id)

    EVENTBRIDGE_EVENTS_RECEIVED.inc()
    logger.info("[SECURITY_HUB] Received valid EventBridge webhook for org %s", sanitize(org_id))

    try:
        # Enqueue background task to process and parse the findings
        process_securityhub_finding.delay(payload, org_id)
    except Exception:
        logger.exception("[SECURITY_HUB] Enqueue failure for org %s", sanitize(org_id))
        EVENTBRIDGE_EVENTS_FAILED.labels(reason="enqueue_failure").inc()
        return jsonify({"error": "Failed to enqueue processing task"}), 500

    return jsonify({"received": True}), 200

@securityhub_bp.route("/findings", methods=["GET"])
@require_permission("connectors", "read")
def get_findings(user_id):
    """
    Fetch AWS Security Hub findings for the authenticated user's organization.
    Supports a limit parameter bounded between 1 and 200.
    """
    org_id = get_org_id_from_request()
    limit = request.args.get('limit', 50, type=int)
    limit = max(1, min(limit, 200))
    
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    """
                    SELECT finding_id, source, title, severity_label, 
                           payload, ai_summary, ai_risk_level, ai_suggested_fix,
                           created_at, updated_at
                    FROM aws_security_findings
                    WHERE org_id = %s
                    ORDER BY updated_at DESC
                    LIMIT %s
                    """,
                    (org_id, limit)
                )
                findings = cursor.fetchall()

        # format records slightly
        formatted_findings = []
        for finding in findings:
            item = dict(finding)
            # Serialize datetimes to string format compatible with frontend JSON if necessary
            for k, v in item.items():
                if hasattr(v, 'isoformat'):
                    item[k] = v.isoformat()
            formatted_findings.append(item)
            
        return jsonify({"findings": formatted_findings}), 200
        
    except Exception as exc:
        logger.exception("[SECURITY_HUB] Failed to fetch findings: %s", exc)
        return jsonify({"error": "Failed to fetch security hub findings"}), 500
