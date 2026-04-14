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
from utils.auth.rbac_decorators import require_auth_only
from utils.auth.stateless_auth import get_org_id_from_request

logger = logging.getLogger(__name__)

securityhub_bp = Blueprint("securityhub", __name__)

EVENTBRIDGE_EVENTS_RECEIVED = Counter(
    "aws_securityhub_events_received_total", 
    "Total EventBridge Security Hub events received",
    ["org_id"]
)
EVENTBRIDGE_EVENTS_FAILED = Counter(
    "aws_securityhub_events_failed_total", 
    "Total EventBridge Security Hub events failed",
    ["org_id", "reason"]
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
        logger.error("[SECURITY_HUB] Failed to validate API key: %s", exc)
        return False

@securityhub_bp.route("/webhook/<org_id>", methods=["POST", "OPTIONS"])
@EVENTBRIDGE_PROCESSING_LATENCY.time()
def webhook(org_id: str):
    if request.method == "OPTIONS":
        return create_cors_response()

    api_key = request.headers.get("x-api-key")
    if not api_key:
        EVENTBRIDGE_EVENTS_FAILED.labels(org_id=org_id, reason="missing_api_key").inc()
        return jsonify({"error": "Missing x-api-keyheader"}), 401

    if not _validate_api_key(org_id, api_key):
        EVENTBRIDGE_EVENTS_FAILED.labels(org_id=org_id, reason="invalid_api_key").inc()
        return jsonify({"error": "Invalid API Key"}), 403

    payload = request.get_json(silent=True)
    if not payload:
        EVENTBRIDGE_EVENTS_FAILED.labels(org_id=org_id, reason="invalid_json").inc()
        return jsonify({"error": "Invalid JSON payload"}), 400

    source = payload.get("source")
    if source != "aws.securityhub":
        EVENTBRIDGE_EVENTS_FAILED.labels(org_id=org_id, reason="invalid_source").inc()
        return jsonify({"error": "Invalid event source. Must be aws.securityhub"}), 400

    EVENTBRIDGE_EVENTS_RECEIVED.labels(org_id=org_id).inc()
    logger.info(f"[SECURITY_HUB] Received valid EventBridge webhook for org {org_id}")

    # Enqueue background task to process and parse the findings
    process_securityhub_finding.delay(payload, org_id)

    return jsonify({"received": True}), 200

@securityhub_bp.route("/findings", methods=["OPTIONS"])
def get_findings_options():
    return create_cors_response()

@securityhub_bp.route("/findings", methods=["GET"])
@require_auth_only
def get_findings(user_id):
    org_id = get_org_id_from_request()
    limit = request.args.get('limit', 50, type=int)
    
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
        logger.error("[SECURITY_HUB] Failed to fetch findings: %s", exc, exc_info=True)
        return jsonify({"error": "Failed to fetch security hub findings"}), 500
