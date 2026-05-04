"""GitHub App webhook ingest endpoint.

Single public ingress for all GitHub App webhook deliveries. Authentication
is the HMAC-SHA256 signature in ``X-Hub-Signature-256``; no Aurora session
or RBAC is required (and would be wrong here, since GitHub doesn't carry
an Aurora user identity).

Flow (each step short-circuits on failure):
    1. Read raw body BEFORE any JSON parse - required for HMAC validity.
    2. Validate the HMAC signature against the Vault-backed secret.
    3. Extract the X-GitHub-Delivery and X-GitHub-Event metadata headers.
    4. INSERT into ``webhook_deliveries``; on UNIQUE conflict, return 200
       with ``{deduped: true}`` (idempotent ack - GitHub retries).
    5. Parse the body and capture ``installation.id`` (best-effort) for
       the audit row.
    6. Enqueue the Celery dispatcher.
    7. Mark the row as ``processing`` and return 200.

Security invariants
-------------------
- Never logs raw body, signature digest, or the secret at INFO.
- Constant-time comparison via ``hmac.compare_digest`` (delegated to the
  Task 5 validator which already enforces this).
- Fails closed on any unexpected internal error (returns 500 with a
  generic message; details go to logs only).

Standard log keys
-----------------
This module emits structured ``key=value`` log lines on the canonical
key ``gh_webhook_event``. The known event values are:

    * ``missing_signature`` — request had no ``X-Hub-Signature-256``.
    * ``secret_unavailable`` — Vault/env webhook secret not configured.
    * ``invalid_signature`` — HMAC mismatch (or malformed sig header).
    * ``missing_metadata`` — required ``X-GitHub-*`` headers absent.
    * ``received``         — body validated, accepted for processing.
    * ``deduped``          — ``delivery_id`` already in DB; idempotent ack.
    * ``dispatched``       — Celery task enqueued.
    * ``handler_error``    — internal failure during DB / dispatch.

Other keys present on these lines:

    * ``delivery_id``     — ``X-GitHub-Delivery`` UUID, or ``-`` if not yet read.
    * ``event_type``      — ``X-GitHub-Event`` value, or ``-`` if not yet read.
    * ``installation_id`` — best-effort capture from payload, may be ``None``.
    * ``duration_ms``     — wall-clock from request entry to log emit.
    * ``error_class``     — exception class name (failure paths only).

Token values are NEVER logged. Any exception text we include in the
``handler_error`` line is passed through ``redact_token()`` first.
"""

from __future__ import annotations

import json
import logging
import time

import flask
from flask import Blueprint, jsonify, request
from psycopg2 import IntegrityError, errors as psycopg_errors

from connectors.github_connector.vault_keys import (
    GitHubAppConfigError,
    get_app_webhook_secret,
)
from utils.auth.github_webhook import (
    GitHubWebhookError,
    SIGNATURE_HEADER,
    extract_delivery_id,
    extract_event_type,
    verify_webhook_signature,
)
from utils.auth.log_redact import redact_token
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

github_webhook_bp = Blueprint("github_webhook", __name__)


@github_webhook_bp.route("/webhook", methods=["POST"])
def github_webhook():
    """Ingest a GitHub App webhook delivery."""
    if not flask.current_app.config.get("GITHUB_APP_ENABLED"):
        return jsonify({"error": "GitHub App not configured. Aurora is in OAuth-only mode."}), 503
    start = time.monotonic()

    # 1. Raw body BEFORE any JSON parse (HMAC requires byte-exact body).
    raw_body = request.get_data(cache=True)

    # 2. Signature header must be present.
    signature_header = request.headers.get(SIGNATURE_HEADER, "")
    if not signature_header:
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.warning(
            "gh_webhook_event=missing_signature delivery_id=- event_type=- "
            "duration_ms=%d",
            duration_ms,
        )
        return jsonify({"error": "missing signature"}), 401

    try:
        webhook_secret = get_app_webhook_secret()
    except GitHubAppConfigError as exc:
        # Misconfiguration on our side, not the sender's fault. Return
        # 503 so GitHub retries instead of giving up on the delivery.
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.error(
            "gh_webhook_event=secret_unavailable delivery_id=- event_type=- "
            "duration_ms=%d error_class=%s",
            duration_ms,
            type(exc).__name__,
        )
        return jsonify({"error": "webhook secret not configured"}), 503

    try:
        is_valid = verify_webhook_signature(raw_body, signature_header, webhook_secret)
    except GitHubWebhookError as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.warning(
            "gh_webhook_event=invalid_signature delivery_id=- event_type=- "
            "duration_ms=%d error_class=%s reason=malformed_header",
            duration_ms,
            type(exc).__name__,
        )
        return jsonify({"error": "invalid signature"}), 401

    if not is_valid:
        # Validator already logged the mismatch; never echo the digest.
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.warning(
            "gh_webhook_event=invalid_signature delivery_id=- event_type=- "
            "duration_ms=%d reason=hmac_mismatch",
            duration_ms,
        )
        return jsonify({"error": "invalid signature"}), 401

    # 3. Metadata headers (only after sig check passes). dict() coercion
    # matches the Mapping[str, str] contract of the Task 5 helpers.
    try:
        headers = dict(request.headers)
        delivery_id = extract_delivery_id(headers)
        event_type = extract_event_type(headers)
    except GitHubWebhookError as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.warning(
            "gh_webhook_event=missing_metadata delivery_id=- event_type=- "
            "duration_ms=%d error_class=%s",
            duration_ms,
            type(exc).__name__,
        )
        return jsonify({"error": "missing webhook metadata"}), 400

    # Signature + metadata both valid — record entry to processing.
    logger.info(
        "gh_webhook_event=received delivery_id=%s event_type=%s",
        delivery_id,
        event_type,
    )

    # 4-7: DB insert with idempotent dedupe + Celery dispatch.
    installation_id: int | None = None
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        """INSERT INTO webhook_deliveries (delivery_id, event_type, status)
                           VALUES (%s, %s, 'received')""",
                        (delivery_id, event_type),
                    )
                except (IntegrityError, psycopg_errors.UniqueViolation):
                    # Duplicate delivery - GitHub retried. Idempotent ack
                    # so it stops retrying. No new Celery dispatch.
                    conn.rollback()
                    duration_ms = int((time.monotonic() - start) * 1000)
                    logger.info(
                        "gh_webhook_event=deduped delivery_id=%s event_type=%s "
                        "installation_id=%s duration_ms=%d",
                        delivery_id,
                        event_type,
                        installation_id,
                        duration_ms,
                    )
                    return jsonify({"deduped": True, "delivery_id": delivery_id}), 200
                conn.commit()

                # Best-effort installation.id capture for the audit row.
                # Sig already verified, so the body is trusted bytes; if
                # it isn't JSON, accept anyway (GitHub may add new types).
                try:
                    payload = json.loads(raw_body or b"{}")
                    if isinstance(payload, dict):
                        inst = payload.get("installation")
                        if isinstance(inst, dict):
                            candidate = inst.get("id")
                            if isinstance(candidate, int):
                                installation_id = candidate
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    logger.warning(
                        "gh_webhook_event=payload_unparseable delivery_id=%s "
                        "event_type=%s error_class=%s",
                        delivery_id,
                        event_type,
                        type(exc).__name__,
                    )

                if installation_id is not None:
                    cur.execute(
                        """UPDATE webhook_deliveries
                           SET installation_id = %s
                           WHERE delivery_id = %s""",
                        (installation_id, delivery_id),
                    )
                    conn.commit()

                # Late import: keeps Flask startup decoupled from Celery
                # broker availability and avoids a circular import at boot.
                from tasks.github_webhook_tasks import dispatch_github_webhook

                # Decode for Celery JSON serializer; payload is the same
                # bytes the sender sent so handlers see the original text.
                payload_json_str = (raw_body or b"").decode("utf-8", errors="replace")
                dispatch_github_webhook.delay(delivery_id, event_type, payload_json_str)

                cur.execute(
                    """UPDATE webhook_deliveries
                       SET status = 'processing'
                       WHERE delivery_id = %s AND status = 'received'""",
                    (delivery_id,),
                )
                conn.commit()
    except Exception as exc:
        # Don't leak details (could include payload fragments). Log here
        # for ops; return generic to caller. ``redact_token`` covers any
        # token-shaped substring an exception message could echo back.
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.exception(
            "gh_webhook_event=handler_error delivery_id=%s event_type=%s "
            "installation_id=%s duration_ms=%d error_class=%s msg=%s",
            delivery_id,
            event_type,
            installation_id,
            duration_ms,
            type(exc).__name__,
            redact_token(str(exc)),
        )
        return jsonify({"error": "internal error"}), 500

    duration_ms = int((time.monotonic() - start) * 1000)
    logger.info(
        "gh_webhook_event=dispatched delivery_id=%s event_type=%s "
        "installation_id=%s duration_ms=%d",
        delivery_id,
        event_type,
        installation_id,
        duration_ms,
    )
    return jsonify({"received": True, "delivery_id": delivery_id}), 200
