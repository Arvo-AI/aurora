"""
CloudWatch Alarm Routes — receives SNS alarm notifications and manages
the CloudWatch connection record for each Aurora user.

AWS SNS delivers CloudWatch alarm state changes as HTTP POSTs containing
either a subscription-confirmation message or an alarm-state-change notification
wrapped in an SNS envelope. This route handles both cases.

Webhook URL format: /aws/cloudwatch/webhook/<user_id>
"""

import base64
import json
import logging
import os
import re
import string
from typing import Tuple
from urllib.parse import urlparse, urlunparse

from flask import Blueprint, jsonify, request

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from routes.aws.cloudwatch_tasks import process_cloudwatch_alarm
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import validate_user_exists, set_rls_context
from utils.auth.token_management import store_tokens_in_db, get_token_data
from utils.db.connection_pool import db_pool
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

cloudwatch_bp = Blueprint("cloudwatch", __name__)

# SNS message types
_SNS_TYPE_HEADER = "x-amz-sns-message-type"
_SNS_SUBSCRIPTION_CONFIRMATION = "SubscriptionConfirmation"
_SNS_NOTIFICATION = "Notification"

# AWS SNS hostname pattern — prevents SSRF by ensuring we only communicate with
# legitimate Amazon-owned endpoints (includes China regions).
_SNS_HOSTNAME_PATTERN = re.compile(
    r"^sns\.[a-z0-9-]+\.amazonaws\.com(\.cn)?$"
)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _is_valid_sns_url(url: str) -> bool:
    """Verify a SubscribeURL is a legitimate AWS SNS endpoint (SSRF prevention).

    Defense-in-depth: parsed URL structural validation ensures only HTTPS
    requests to genuine SNS hostnames are issued.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return False
    if parsed.username or parsed.password:
        return False
    hostname = parsed.hostname or ""
    if not _SNS_HOSTNAME_PATTERN.match(hostname):
        return False
    if parsed.fragment:
        return False
    return True


# Cache fetched/parsed certificates with TTL to avoid re-downloading per message
# while ensuring rotated certs are eventually refreshed.
_CERT_CACHE_TTL = 3600  # 1 hour
_CERT_CACHE_MAX_SIZE = 32
_cert_cache: dict = {}  # url -> (cert, timestamp)


def _sanitize_signing_cert_url(url: str) -> tuple[str, str] | None:
    """Validate SigningCertURL and extract (hostname, path) for safe reconstruction.

    Returns validated (hostname, path) tuple on success, or None if validation fails.
    Prevents SSRF by only extracting parts that pass the allowlist — the caller
    reconstructs the URL from a hardcoded template.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return None
    hostname = parsed.hostname or ""
    if not _SNS_HOSTNAME_PATTERN.match(hostname):
        return None
    path = parsed.path or ""
    if not path.endswith(".pem"):
        return None
    if len(path) > 256:
        return None
    allowed = set(string.ascii_letters + string.digits + "._/-")
    if not path.startswith("/") or not all(c in allowed for c in path):
        return None
    return (hostname, path)


def _get_sns_certificate(hostname: str, path: str):
    """Fetch and parse the X.509 certificate from a validated SNS endpoint."""
    import time as _time

    safe_url = f"https://{hostname}{path}"
    cached = _cert_cache.get(safe_url)
    if cached:
        cert, ts = cached
        if _time.monotonic() - ts < _CERT_CACHE_TTL:
            return cert

    import urllib.request
    with urllib.request.urlopen(safe_url, timeout=10) as resp:  # noqa: S310
        pem_data = resp.read()

    cert = x509.load_pem_x509_certificate(pem_data)

    if len(_cert_cache) >= _CERT_CACHE_MAX_SIZE:
        _cert_cache.clear()
    _cert_cache[safe_url] = (cert, _time.monotonic())
    return cert


def _build_sns_string_to_sign(message: dict) -> str:
    """Build the canonical string-to-sign for SNS message verification.

    AWS SNS signing uses a specific set of fields in alphabetical order,
    depending on the message type.
    """
    msg_type = message.get("Type", "")

    if msg_type == "Notification":
        fields = ["Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"]
    else:
        fields = ["Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type"]

    pairs = []
    for field in fields:
        value = message.get(field)
        if value is not None:
            pairs.append(f"{field}\n{value}\n")

    return "".join(pairs)


def _verify_sns_signature(message: dict) -> bool:
    """Verify the cryptographic signature of an SNS message.

    Returns True if the signature is valid, False otherwise.
    """
    cert_url = message.get("SigningCertURL") or message.get("SigningCertUrl") or ""
    signature_b64 = message.get("Signature") or ""
    signature_version = message.get("SignatureVersion", "1")

    if not cert_url or not signature_b64:
        return False

    safe_cert_url = _sanitize_signing_cert_url(cert_url)
    if not safe_cert_url:
        logger.warning("[CLOUDWATCH] Invalid SigningCertURL: rejected")
        return False

    try:
        cert = _get_sns_certificate(*safe_cert_url)
        signature = base64.b64decode(signature_b64)
        string_to_sign = _build_sns_string_to_sign(message)

        public_key = cert.public_key()

        if signature_version == "2":
            public_key.verify(
                signature,
                string_to_sign.encode("utf-8"),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        else:
            # AWS SNS SignatureVersion 1 mandates SHA1 for verification — not a design choice.
            public_key.verify(  # nosec B303
                signature,
                string_to_sign.encode("utf-8"),
                padding.PKCS1v15(),
                hashes.SHA1(),  # NOSONAR — AWS protocol-mandated, signature verification only
            )

        return True
    except Exception as exc:
        logger.warning("[CLOUDWATCH] SNS signature verification failed: %s", type(exc).__name__)
        return False


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _has_cloudwatch_row(user_id: str) -> Tuple[bool, bool]:
    """Return (row_exists, is_active) for the user's CloudWatch token row."""
    try:
        from utils.db.org_scope import resolve_org, org_read_predicate
        org_id = resolve_org(user_id)
        predicate, pred_params = org_read_predicate(user_id, org_id)
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix="[CLOUDWATCH:has_row]")
                cursor.execute(
                    f"SELECT is_active FROM user_tokens "
                    f"WHERE {predicate} AND provider = 'cloudwatch' "
                    f"ORDER BY is_active DESC LIMIT 1",
                    (*pred_params,),
                )
                row = cursor.fetchone()
                if row is None:
                    return False, False
                return True, bool(row[0])
    except Exception:
        logger.exception("[CLOUDWATCH] Failed to check user_tokens row")
        return False, False


def _set_cloudwatch_active(user_id: str, active: bool) -> bool:
    """Flip is_active on the existing CloudWatch user_tokens row."""
    try:
        from utils.db.org_scope import resolve_org, org_read_predicate
        org_id = resolve_org(user_id)
        predicate, pred_params = org_read_predicate(user_id, org_id)
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix="[CLOUDWATCH:set_active]")
                cursor.execute(
                    f"UPDATE user_tokens SET is_active = %s, timestamp = CURRENT_TIMESTAMP "
                    f"WHERE {predicate} AND provider = 'cloudwatch'",
                    (active, *pred_params),
                )
                updated = cursor.rowcount > 0
            conn.commit()
            return updated
    except Exception:
        logger.exception("[CLOUDWATCH] Failed to set is_active=%s", active)
        return False


# ---------------------------------------------------------------------------
# TopicArn binding helpers
# ---------------------------------------------------------------------------

def _get_approved_topic_arn(user_id: str) -> str | None:
    """Retrieve the approved TopicArn stored for this user's CloudWatch connection."""
    try:
        data = get_token_data(user_id, "cloudwatch")
        if data:
            return data.get("approved_topic_arn")
    except Exception:
        logger.debug("[CLOUDWATCH] Could not retrieve token data for topic check")
    return None


def _store_approved_topic(user_id: str, topic_arn: str) -> None:
    """Persist the TopicArn as the approved source for this user's CloudWatch webhook."""
    try:
        existing = get_token_data(user_id, "cloudwatch") or {}
        existing["approved_topic_arn"] = topic_arn
        store_tokens_in_db(user_id, existing, "cloudwatch")
        logger.info(
            "[CLOUDWATCH] Bound TopicArn for user %s",
            sanitize(user_id),
        )
    except Exception:
        logger.exception("[CLOUDWATCH] Failed to persist TopicArn binding for user %s", sanitize(user_id))


def _validate_topic_arn(user_id: str, sns_message: dict) -> bool:
    """Validate the TopicArn in an SNS message against the stored binding.

    On first interaction (no binding stored yet), the topic is trusted and persisted.
    On subsequent interactions, the topic must match.
    Returns True if the message should be processed, False to reject.
    """
    topic_arn = sns_message.get("TopicArn") or ""
    if not topic_arn:
        logger.warning("[CLOUDWATCH] SNS message missing TopicArn for user %s", sanitize(user_id))
        return False

    approved = _get_approved_topic_arn(user_id)
    if approved is None:
        _store_approved_topic(user_id, topic_arn)
        # Re-read to guard against a concurrent write binding a different topic.
        stored = _get_approved_topic_arn(user_id)
        if stored and stored != topic_arn:
            logger.warning(
                "[CLOUDWATCH] TopicArn race for user %s: rejected unauthorized topic",
                sanitize(user_id),
            )
            return False
        return True

    if topic_arn != approved:
        logger.warning(
            "[CLOUDWATCH] TopicArn mismatch for user %s: rejected unauthorized topic",
            sanitize(user_id),
        )
        return False

    return True


# ---------------------------------------------------------------------------
# Webhook helpers
# ---------------------------------------------------------------------------

def _handle_subscription_confirmation(sns_message: dict, user_id: str):
    """Process SNS SubscriptionConfirmation by visiting the SubscribeURL.

    SSRF mitigation: _is_valid_sns_url() enforces that the URL is an HTTPS
    endpoint on sns.<region>.amazonaws.com(.cn) with no embedded credentials,
    fragments, or suspicious path components. We additionally reconstruct the
    URL from its parsed components to neutralize any encoding tricks.
    """
    subscribe_url = sns_message.get("SubscribeURL") or ""
    if subscribe_url and _is_valid_sns_url(subscribe_url):
        import urllib.request
        parsed = urlparse(subscribe_url)
        safe_url = urlunparse((parsed.scheme, parsed.hostname, parsed.path, "", parsed.query, ""))
        try:
            with urllib.request.urlopen(safe_url, timeout=10) as resp:  # noqa: S310
                status_code = int(resp.status)
                logger.info(
                    "[CLOUDWATCH] Auto-confirmed SNS subscription for user %s (status=%d)",
                    sanitize(user_id), status_code,
                )
        except Exception as exc:
            logger.warning(
                "[CLOUDWATCH] Failed to confirm SNS subscription for user %s: %s",
                sanitize(user_id), type(exc).__name__,
            )
    elif subscribe_url:
        logger.warning(
            "[CLOUDWATCH] Rejected non-SNS SubscribeURL for user %s",
            sanitize(user_id),
        )


def _ensure_cloudwatch_connection(user_id: str) -> Tuple[bool, bool, bool]:
    """Ensure a CloudWatch connection exists. Returns (skip_rca, should_skip, is_error).

    Design: On first-ever webhook for a user, we auto-create the connection record
    so users don't need to manually toggle "connect" before configuring SNS. The
    webhook is protected by SNS signature verification + validate_user_exists, so
    this cannot be triggered by arbitrary external requests. RCA is skipped for the
    auto-connect event to avoid investigating stale/test alarms during initial setup.

    Unlike Grafana, we intentionally do NOT re-activate a disabled connection on
    webhook receipt — if a user explicitly disconnected, we respect that choice.

    - If the row doesn't exist, auto-creates it and returns (True, False, False).
    - If the row exists but is inactive, returns (False, True, False).
    - If the row exists and is active, returns (False, False, False).
    - On creation failure, returns (False, True, True) — caller should return 500.
    """
    row_exists, is_active = _has_cloudwatch_row(user_id)

    if not row_exists:
        logger.info("[CLOUDWATCH] Auto-connecting user %s via webhook", sanitize(user_id))
        try:
            existing = get_token_data(user_id, "cloudwatch") or {}
            store_tokens_in_db(user_id, existing, "cloudwatch")
        except Exception:
            logger.exception("[CLOUDWATCH] Failed to auto-connect user %s", sanitize(user_id))
            return False, True, True
        return True, False, False

    if not is_active:
        logger.info(
            "[CLOUDWATCH] Webhook received for user %s but connection is disabled, skipping",
            sanitize(user_id),
        )
        return False, True, False

    return False, False, False


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@cloudwatch_bp.route("/aws/cloudwatch/status", methods=["GET"])
@require_permission("connectors", "read")
def status(user_id):
    """Return whether the CloudWatch integration is active for this user."""
    row_exists, is_active = _has_cloudwatch_row(user_id)
    if not row_exists or not is_active:
        return jsonify({"connected": False})
    return jsonify({"connected": True})


@cloudwatch_bp.route("/aws/cloudwatch/connect", methods=["POST"])
@require_permission("connectors", "write")
def connect(user_id):
    """Activate (or create) the CloudWatch connection record."""
    try:
        row_exists, is_active = _has_cloudwatch_row(user_id)
        if row_exists and is_active:
            return jsonify({"success": True, "message": "Already connected"}), 200
        if row_exists:
            if _set_cloudwatch_active(user_id, True):
                logger.info("[CLOUDWATCH] Re-activated for user %s", sanitize(user_id))
                return jsonify({"success": True, "message": "CloudWatch re-activated"}), 200
            return jsonify({"error": "Failed to activate CloudWatch"}), 500
        store_tokens_in_db(user_id, {}, "cloudwatch")
        logger.info("[CLOUDWATCH] Connected for user %s", sanitize(user_id))
        return jsonify({"success": True, "message": "CloudWatch connected"}), 200
    except Exception:
        logger.exception("[CLOUDWATCH] Failed to connect")
        return jsonify({"error": "Failed to connect CloudWatch"}), 500


@cloudwatch_bp.route("/aws/cloudwatch/disconnect", methods=["POST", "DELETE"])
@require_permission("connectors", "write")
def disconnect(user_id):
    """Deactivate (but retain) the CloudWatch connection record."""
    try:
        row_exists, is_active = _has_cloudwatch_row(user_id)
        if not row_exists:
            return jsonify({"success": True, "message": "No connection to disconnect"}), 200
        if not is_active:
            return jsonify({"success": True, "message": "Already disconnected"}), 200
        if _set_cloudwatch_active(user_id, False):
            logger.info("[CLOUDWATCH] Disconnected for user %s", sanitize(user_id))
            return jsonify({"success": True, "message": "CloudWatch disconnected successfully"}), 200
        return jsonify({"error": "Failed to disconnect CloudWatch"}), 500
    except Exception:
        logger.exception("[CLOUDWATCH] Failed to disconnect")
        return jsonify({"error": "Failed to disconnect CloudWatch"}), 500


@cloudwatch_bp.route("/aws/cloudwatch/webhook-url", methods=["GET"])
@require_permission("connectors", "read")
def get_webhook_url(user_id):
    """Return the SNS subscription URL the user should configure in AWS."""
    from utils.secrets.secret_ref_utils import get_token_owner_id
    webhook_owner_id = get_token_owner_id(user_id, "cloudwatch")

    ngrok_url = os.getenv("NGROK_URL", "").rstrip("/")
    backend_url = os.getenv("NEXT_PUBLIC_BACKEND_URL", "").rstrip("/")

    if ngrok_url and backend_url.startswith("http://localhost"):
        base_url = ngrok_url
    else:
        base_url = backend_url

    webhook_url = f"{base_url}/aws/cloudwatch/webhook/{webhook_owner_id}"

    return jsonify({
        "webhookUrl": webhook_url,
    })


def _parse_notification_payload(sns_message: dict) -> dict:
    """Extract the alarm payload from an SNS notification envelope."""
    raw_message = sns_message.get("Message") or ""
    try:
        return json.loads(raw_message) if raw_message else sns_message
    except Exception:
        return sns_message


@cloudwatch_bp.route("/aws/cloudwatch/webhook/<user_id>", methods=["POST"])
def cloudwatch_alarm_webhook(user_id: str):
    """Receive SNS alarm notifications from CloudWatch for a specific user.

    Handles two SNS message types:
    - SubscriptionConfirmation: visit the SubscribeURL to confirm the subscription.
    - Notification: parse the CloudWatch alarm payload and process it.

    Auto-creates the connection record on first-ever notification. If the user
    has explicitly disabled the connection, webhooks are acknowledged but skipped.
    """
    if not validate_user_exists(user_id):
        return jsonify({"error": "Unknown user"}), 404

    body = request.get_data(as_text=True)
    try:
        sns_message = json.loads(body)
    except Exception:
        logger.warning("[CLOUDWATCH] Could not parse webhook body for user %s", sanitize(user_id))
        return jsonify({"error": "Invalid JSON body"}), 400

    if not _verify_sns_signature(sns_message):
        logger.warning(
            "[CLOUDWATCH] SNS signature verification failed for user %s", sanitize(user_id)
        )
        return jsonify({"error": "Invalid SNS signature"}), 403

    if not _validate_topic_arn(user_id, sns_message):
        return jsonify({"error": "TopicArn not authorized"}), 403

    sns_type = (
        request.headers.get(_SNS_TYPE_HEADER)
        or sns_message.get("Type")
        or ""
    )

    if sns_type == _SNS_SUBSCRIPTION_CONFIRMATION:
        _handle_subscription_confirmation(sns_message, user_id)
        return jsonify({"received": True, "type": "subscription_confirmed"})

    if sns_type not in (_SNS_NOTIFICATION, ""):
        logger.info(
            "[CLOUDWATCH] Unknown SNS type=%s for user %s, ignoring", sns_type, sanitize(user_id)
        )
        return jsonify({"received": True})

    payload = _parse_notification_payload(sns_message)

    skip_rca, should_skip, is_error = _ensure_cloudwatch_connection(user_id)
    if should_skip:
        if is_error:
            return jsonify({"error": "Failed to create CloudWatch connection"}), 500
        return jsonify({"received": True, "skipped": True, "reason": "connection_disabled"})

    alarm_name = payload.get("AlarmName") or "unknown"
    state_value = payload.get("NewStateValue") or payload.get("state_value") or "unknown"
    logger.info(
        "[CLOUDWATCH] Received alarm webhook for user %s: %s (state=%s)",
        sanitize(user_id), sanitize(alarm_name), sanitize(state_value),
    )

    sns_message_id = sns_message.get("MessageId") or ""
    metadata = {
        "headers": dict(request.headers),
        "remote_addr": request.remote_addr,
        "sns_message_id": sns_message_id,
    }

    process_cloudwatch_alarm.delay(payload, metadata, user_id, skip_rca=skip_rca)

    return jsonify({"received": True})
