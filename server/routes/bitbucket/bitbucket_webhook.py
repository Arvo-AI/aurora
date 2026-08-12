"""Bitbucket Cloud webhook ingest endpoint (Incident Prevention).

Single public ingress for Bitbucket repo webhook deliveries, org-scoped:
``POST /bitbucket/webhook/<org_id>``. Authentication is the HMAC-SHA256
signature Bitbucket sends in **``X-Hub-Signature``** (``sha256=<hex>``,
over the raw body) — note this is NOT GitHub's ``X-Hub-Signature-256``
header name. No Aurora session or RBAC is required (Bitbucket carries no
Aurora identity); the org id in the path selects which org's secret
validates the delivery.

Flow (each step short-circuits on failure):
    1. Feature flag check (``NEXT_PUBLIC_ENABLE_INCIDENT_PREVENTION``).
    2. Read raw body BEFORE any JSON parse — required for HMAC validity.
    3. Load the org's webhook secret (secrets backend via
       ``organizations.bitbucket_webhook_secret_ref``); validate the HMAC.
    4. Extract ``X-Request-UUID`` (delivery id) and ``X-Event-Key`` (event).
    5. INSERT into ``webhook_deliveries``; on UNIQUE conflict with a
       completed prior attempt, return 200 ``{deduped: true}``.
    6. Enqueue the Celery dispatcher; mark the row ``processing``.

Security invariants (mirrors ``routes/github/github_webhook.py``):
- Never logs raw body, signature digest, or the secret.
- Constant-time comparison via ``hmac.compare_digest``.
- Fails closed on any unexpected internal error (500, details in logs).

Structured log key: ``bb_webhook_event`` with the same event vocabulary
as ``gh_webhook_event`` (missing_signature / secret_unavailable /
invalid_signature / missing_metadata / received / deduped / dispatched /
handler_error).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import time

from flask import Blueprint, jsonify, request
from psycopg2 import IntegrityError, errors as psycopg_errors

from utils.auth.log_redact import redact_token
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

bitbucket_webhook_bp = Blueprint("bitbucket_webhook", __name__)

SIGNATURE_HEADER = "X-Hub-Signature"
DELIVERY_HEADER = "X-Request-UUID"
EVENT_HEADER = "X-Event-Key"
_SIGNATURE_PREFIX = "sha256="

# org ids are UUID-shaped strings; the path segment is attacker-chosen, so
# reject anything else BEFORE it reaches logs or query parameters.
_ORG_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_SAFE_LOG_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_:."
)


def _safe_log_value(value: str, max_len: int = 64) -> str:
    """Neutralize a request-supplied value for structured logging.

    Rebuilds the string from an explicit character allowlist (log-injection
    chars like newlines/spaces are dropped, not just escaped) and caps the
    length. Applied to org_id / delivery_id / event_type — the only
    caller-controlled values these logs carry.
    """
    return "".join(c for c in (value or "") if c in _SAFE_LOG_CHARS)[:max_len]


def verify_bitbucket_signature(raw_body: bytes, signature_header: str, secret: str) -> bool:
    """Constant-time HMAC-SHA256 check of Bitbucket's ``X-Hub-Signature``.

    Returns True/False; malformed headers (missing prefix / empty digest)
    return False rather than raising — the caller responds 401 either way.
    """
    if not signature_header or not signature_header.startswith(_SIGNATURE_PREFIX):
        return False
    received = signature_header[len(_SIGNATURE_PREFIX):].strip()
    if not received:
        return False
    expected = hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, received)


def _authenticate_delivery(org_id: str, raw_body: bytes, start: float):
    """Steps 2-4: signature + org secret + metadata headers.

    Returns ``((delivery_id, event_type), None)`` on success or
    ``(None, (json_response, http_status))`` on rejection. ``org_id`` is
    already allowlist-validated by the caller, so it is log-safe here.
    """
    def _reject(event: str, message: str, status: int):
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.warning(
            "bb_webhook_event=%s org_id=%s delivery_id=- event_type=- duration_ms=%d",
            event, org_id, duration_ms,
        )
        return (None, (jsonify({"error": message}), status))

    signature_header = request.headers.get(SIGNATURE_HEADER, "")
    if not signature_header:
        return _reject("missing_signature", "missing signature", 401)

    # Org secret. Absent secret = this org never enabled Incident
    # Prevention → the delivery cannot be authenticated → 401 (NOT 503:
    # the org id is attacker-chosen, so an unknown org must not read as
    # "server misconfigured, retry").
    from connectors.bitbucket_connector.webhook_secret import get_webhook_secret

    webhook_secret = get_webhook_secret(org_id)
    if not webhook_secret:
        return _reject("secret_unavailable", "unknown webhook endpoint", 401)

    if not verify_bitbucket_signature(raw_body, signature_header, webhook_secret):
        return _reject("invalid_signature", "invalid signature", 401)

    # Metadata headers (only after sig check passes). Values are request-
    # supplied: allowlist-sanitize before they can reach any log line.
    delivery_id = _safe_log_value(request.headers.get(DELIVERY_HEADER, "").strip())
    event_type = _safe_log_value(request.headers.get(EVENT_HEADER, "").strip(), 40)
    if not delivery_id or not event_type:
        return _reject("missing_metadata", "missing webhook metadata", 400)
    # Bitbucket retries reuse the same X-Request-UUID; namespace by org so
    # a delivery id can never collide across providers/orgs in the shared
    # webhook_deliveries table (column is VARCHAR(64); uuid+prefix fits).
    return ((f"bb:{delivery_id}"[:64], event_type), None)


def _record_and_dispatch(
    org_id: str, delivery_id: str, event_type: str, raw_body: bytes, start: float
):
    """Steps 5-6: idempotent ``webhook_deliveries`` insert + Celery dispatch.

    Same status lifecycle as the GitHub ingress (pending → processing →
    processed / failed) so a broker hiccup never permanently drops a
    delivery: the retry lands on the duplicate branch and re-dispatches
    pending/failed rows. Returns a ``(response, status)`` pair or None to
    let the caller emit the standard dispatched ack.
    """
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    """INSERT INTO webhook_deliveries (delivery_id, event_type, status)
                       VALUES (%s, %s, 'pending')""",
                    (delivery_id, event_type),
                )
                conn.commit()
                is_duplicate = False
            except (IntegrityError, psycopg_errors.UniqueViolation):
                conn.rollback()
                is_duplicate = True

            if is_duplicate:
                cur.execute(
                    "SELECT status FROM webhook_deliveries WHERE delivery_id = %s",
                    (delivery_id,),
                )
                existing = cur.fetchone()
                existing_status = existing[0] if existing else None
                if existing_status in (None, "processing", "processed"):
                    duration_ms = int((time.monotonic() - start) * 1000)
                    logger.info(
                        "bb_webhook_event=deduped org_id=%s delivery_id=%s "
                        "event_type=%s duration_ms=%d existing_status=%s",
                        org_id, delivery_id, event_type, duration_ms,
                        existing_status or "missing",
                    )
                    return jsonify({"deduped": True, "delivery_id": delivery_id}), 200
                logger.info(
                    "bb_webhook_event=redispatch org_id=%s delivery_id=%s "
                    "event_type=%s previous_status=%s",
                    org_id, delivery_id, event_type, existing_status,
                )

            # Atomic claim before enqueue — a concurrent retry that
            # already claimed the row must not double-dispatch.
            cur.execute(
                """UPDATE webhook_deliveries
                   SET status = 'processing'
                   WHERE delivery_id = %s
                     AND status IN ('pending', 'failed')""",
                (delivery_id,),
            )
            claimed = cur.rowcount > 0
            conn.commit()

            if not claimed:
                logger.info(
                    "bb_webhook_event=already_claimed org_id=%s delivery_id=%s "
                    "event_type=%s",
                    org_id, delivery_id, event_type,
                )
                return None

            # Late import: keeps Flask startup decoupled from Celery
            # broker availability and avoids a circular import at boot.
            from tasks.bitbucket_webhook_tasks import dispatch_bitbucket_webhook

            payload_json_str = (raw_body or b"").decode("utf-8", errors="replace")
            try:
                dispatch_bitbucket_webhook.delay(
                    org_id, delivery_id, event_type, payload_json_str
                )
            except Exception as dispatch_exc:
                cur.execute(
                    """UPDATE webhook_deliveries
                        SET status = 'failed',
                            error = %s
                        WHERE delivery_id = %s""",
                    (
                        redact_token(
                            f"{type(dispatch_exc).__name__}: {dispatch_exc}"
                        )[:1024],
                        delivery_id,
                    ),
                )
                conn.commit()
                raise
    return None


@bitbucket_webhook_bp.route("/webhook/<org_id>", methods=["POST"])
def bitbucket_webhook(org_id: str):
    """Ingest a Bitbucket repo webhook delivery for one org."""
    from utils.flags.feature_flags import is_incident_prevention_enabled

    if not is_incident_prevention_enabled():
        return jsonify({"error": "Incident Prevention is disabled."}), 503
    if not _ORG_ID_RE.match(org_id or ""):
        # Reject BEFORE org_id can reach any log line or query parameter.
        return jsonify({"error": "invalid org id"}), 400
    # Rebuild through the allowlist (a no-op after the regex check) so the
    # value that reaches logs is provably detached from raw request input.
    org_id = _safe_log_value(org_id)
    start = time.monotonic()

    # Raw body BEFORE any JSON parse (HMAC requires byte-exact body).
    raw_body = request.get_data(cache=True)

    auth, rejection = _authenticate_delivery(org_id, raw_body, start)
    if rejection is not None:
        return rejection
    delivery_id, event_type = auth

    logger.info(
        "bb_webhook_event=received org_id=%s delivery_id=%s event_type=%s",
        org_id, delivery_id, event_type,
    )

    try:
        early_response = _record_and_dispatch(
            org_id, delivery_id, event_type, raw_body, start
        )
        if early_response is not None:
            return early_response
    except Exception as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.exception(
            "bb_webhook_event=handler_error org_id=%s delivery_id=%s "
            "event_type=%s duration_ms=%d error_class=%s msg=%s",
            org_id, delivery_id, event_type, duration_ms,
            type(exc).__name__, redact_token(str(exc)),
        )
        return jsonify({"error": "internal error"}), 500

    duration_ms = int((time.monotonic() - start) * 1000)
    logger.info(
        "bb_webhook_event=dispatched org_id=%s delivery_id=%s event_type=%s "
        "duration_ms=%d",
        org_id, delivery_id, event_type, duration_ms,
    )
    return jsonify({"received": True, "delivery_id": delivery_id}), 200
