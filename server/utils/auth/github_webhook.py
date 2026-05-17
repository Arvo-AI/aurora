"""GitHub webhook signature validator and metadata header helpers.

Provides constant-time HMAC-SHA256 verification of GitHub webhook payloads
plus tiny helpers for the two webhook metadata headers Aurora cares about
(``X-GitHub-Delivery`` for idempotency, ``X-GitHub-Event`` for routing).

Security notes
--------------
- Always uses :func:`hmac.compare_digest` to avoid timing side-channels;
  ``==`` MUST never be used for signature comparison.
- The validator MUST be called against the **raw request body** before any
  JSON / form parsing — re-serialising would change byte-for-byte content
  and break the HMAC.
- Secrets, signatures, and payload bytes are never logged. Errors carry
  only the failure category, never sensitive material.
- ``verify_webhook_signature`` returns ``True``/``False`` for valid /
  invalid signatures; only missing or malformed ``X-Hub-Signature-256``
  metadata raises :class:`GitHubWebhookError`. The caller is expected to
  respond ``401`` on ``False`` or malformed-header errors.

Reference: https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries
"""

import hashlib
import hmac
import logging
from collections.abc import Mapping

logger = logging.getLogger(__name__)

SIGNATURE_HEADER = "X-Hub-Signature-256"
DELIVERY_HEADER = "X-GitHub-Delivery"
EVENT_HEADER = "X-GitHub-Event"

_SIGNATURE_PREFIX = "sha256="


class GitHubWebhookError(Exception):
    """Raised when a GitHub webhook request is missing or malformed metadata.

    This is intentionally distinct from a signature mismatch: a mismatch
    yields ``False`` from :func:`verify_webhook_signature` so the caller
    can choose how to respond, while malformed/missing headers indicate
    the request never came from a well-formed webhook delivery and the
    caller should reject it (typically with HTTP 400 / 401).
    """


def verify_webhook_signature(raw_body: bytes, signature_header: str, secret: str) -> bool:
    """Verify a GitHub webhook ``X-Hub-Signature-256`` header.

    Args:
        raw_body: The exact bytes received in the request body. Must NOT
            be re-serialised JSON — pass ``request.get_data(cache=True)``
            BEFORE Flask parses the payload.
        signature_header: Value of the ``X-Hub-Signature-256`` header,
            expected to look like ``sha256=<hex digest>``.
        secret: The shared webhook secret configured in the GitHub App.

    Returns:
        ``True`` if the signature is valid for the given body+secret,
        ``False`` if the signature is well-formed but does not match.

    Raises:
        GitHubWebhookError: When ``signature_header`` is missing, empty,
            or otherwise malformed (e.g. wrong prefix, no digest body).
            The caller should treat this as an authentication failure.
    """
    if not signature_header:
        # Don't echo the header value (it's empty here, but keep the
        # error message generic so future call sites can't accidentally
        # log a partial signature on malformed input).
        raise GitHubWebhookError("Missing X-Hub-Signature-256 header")

    if not signature_header.startswith(_SIGNATURE_PREFIX):
        raise GitHubWebhookError("Malformed X-Hub-Signature-256 header: missing sha256= prefix")

    received_digest = signature_header[len(_SIGNATURE_PREFIX):]
    if not received_digest:
        raise GitHubWebhookError("Malformed X-Hub-Signature-256 header: empty digest")

    expected_digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()

    # Constant-time comparison guards against timing oracles; compare_digest
    # tolerates unequal-length inputs without leaking length information.
    is_valid = hmac.compare_digest(expected_digest, received_digest)

    if not is_valid:
        # Log only the fact of failure — never the digest values, which
        # would let an attacker observing logs iterate towards a forgery.
        logger.warning("GitHub webhook signature mismatch")

    return is_valid


def _get_header_case_insensitive(headers: Mapping[str, str], name: str) -> str:
    """Look up ``name`` in ``headers`` matching either exact or any case.

    Flask's ``request.headers`` is already case-insensitive, but plain
    dicts (used by Celery tasks and unit tests) are not. This helper
    normalises both call sites.
    """
    value = headers.get(name)
    if value:
        return value

    lowered = name.lower()
    for key, val in headers.items():
        if key.lower() == lowered:
            return val
    return ""


def extract_delivery_id(headers: Mapping[str, str]) -> str:
    """Return the ``X-GitHub-Delivery`` UUID used for idempotency dedupe.

    Args:
        headers: A mapping of incoming HTTP headers (Flask ``request.headers``
            or a plain dict).

    Returns:
        The non-empty delivery ID string.

    Raises:
        GitHubWebhookError: If the header is missing or empty.
    """
    delivery_id = _get_header_case_insensitive(headers, DELIVERY_HEADER)
    if not delivery_id:
        raise GitHubWebhookError(f"Missing {DELIVERY_HEADER} header")
    return delivery_id


def extract_event_type(headers: Mapping[str, str]) -> str:
    """Return the ``X-GitHub-Event`` value used to route the webhook.

    Args:
        headers: A mapping of incoming HTTP headers.

    Returns:
        The non-empty event type string (e.g. ``"pull_request"``).

    Raises:
        GitHubWebhookError: If the header is missing or empty.
    """
    event_type = _get_header_case_insensitive(headers, EVENT_HEADER)
    if not event_type:
        raise GitHubWebhookError(f"Missing {EVENT_HEADER} header")
    return event_type
