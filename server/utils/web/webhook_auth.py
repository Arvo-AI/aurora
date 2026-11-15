"""Per-connection shared secret for inbound alert webhooks.

Alert webhook URLs look like ``/<vendor>/webhook/<user_id>``. The ``user_id``
only says which connection an alert belongs to: every member of an org can read
every other member's id from ``GET /api/orgs/current``, so it is not a
credential. The sender is authenticated by the ``webhook_secret`` stored with
the connector's credentials, compared in constant time.

Accepted locations: the ``X-Aurora-Webhook-Secret`` header, an
``Authorization: Bearer`` token, the HTTP Basic password, or a ``?secret=``
query parameter for alerting tools that can only be given a URL. A request is
accepted when any of them carries the secret, so a vendor that sends its own
``Authorization`` header does not hide a secret in the URL.
"""

import hmac
import logging
import secrets
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple
from urllib.parse import urlencode

from flask import Response, jsonify

from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

WEBHOOK_HEADER_NAME = "X-Aurora-Webhook-Secret"
WEBHOOK_QUERY_PARAM = "secret"
SECRET_KEY = "webhook_secret"

_SENSITIVE_HEADERS = {
    "authorization", "cookie", "set-cookie", "proxy-authorization", "api-key", "x-api-key", "x-csrf-token",
}
_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext(%s))"


def generate_webhook_secret() -> str:
    return secrets.token_urlsafe(32)


def _read_creds_strict(user_id: str, provider: str) -> Dict[str, Any]:
    """Stored credentials, or {} when none are stored. Raises when the store can't be read.

    A read error must not look like "no secret yet": that would replace the
    secret and silently break every webhook already configured in the vendor.
    """
    from utils.auth.token_management import get_token_data

    return get_token_data(user_id, provider, raise_on_error=True) or {}


def with_webhook_secret(user_id: str, provider: str, token_payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return ``token_payload`` carrying the stored secret, or a new one when none is stored.

    A reconnect or token rotation keeps the secret, so the webhook already
    configured in the vendor keeps working. Raises when the stored credentials
    can't be read; callers store inside their existing error handling.
    """
    secret = _read_creds_strict(user_id, provider).get(SECRET_KEY) or generate_webhook_secret()
    return {**token_payload, SECRET_KEY: secret}


@contextmanager
def webhook_secret_lock(user_id: str, provider: str) -> Iterator[None]:
    """Serialise secret writes for one connection (credentials are org-shared, so the key is the org).

    Holds a transaction-scoped advisory lock; the transaction is rolled back on
    exit, which releases it.
    """
    from utils.auth.stateless_auth import resolve_org_id
    from utils.db.connection_pool import db_pool

    key = f"webhook-secret:{resolve_org_id(user_id) or user_id}:{provider}"
    with db_pool.get_admin_connection() as conn, conn.cursor() as cursor:
        cursor.execute(_LOCK_SQL, (key,))
        try:
            yield
        finally:
            conn.rollback()


def _store(user_id: str, provider: str, payload: Dict[str, Any]) -> None:
    from utils.auth.token_management import store_tokens_in_db
    from utils.secrets.secret_ref_utils import get_token_owner_id

    # Org-shared credentials are written back under the row's owner, as token refresh does.
    store_tokens_in_db(get_token_owner_id(user_id, provider), payload, provider)


def ensure_webhook_secret(
    user_id: str,
    provider: str,
    creds: Mapping[str, Any],
    *,
    allow_new_connection: bool = False,
    new_secret_fields: Optional[Mapping[str, Any]] = None,
) -> Optional[str]:
    """Return the connection's secret, adding one to a connection saved before secrets existed.

    ``allow_new_connection`` creates the connection when nothing is stored
    (Grafana has no credentials to connect with). ``new_secret_fields`` are
    stored alongside a newly created secret. Returns None when the connection
    is gone or the secret could not be stored.
    """
    secret = creds.get(SECRET_KEY)
    if secret:
        return secret
    try:
        with webhook_secret_lock(user_id, provider):
            # Re-read under the lock: a concurrent request may have created it,
            # and the blob written back must be the latest one.
            current = _read_creds_strict(user_id, provider)
            if current.get(SECRET_KEY):
                return current[SECRET_KEY]
            if not current and not allow_new_connection:
                return None
            secret = generate_webhook_secret()
            _store(user_id, provider, {**current, **(new_secret_fields or {}), SECRET_KEY: secret})
    except Exception:
        logger.exception("[WEBHOOK-AUTH] Failed to store a webhook secret for provider %s", sanitize(provider))
        return None
    logger.info("[WEBHOOK-AUTH] Added a webhook secret to a %s connection", sanitize(provider))
    return secret


def rotate_webhook_secret(user_id: str, provider: str) -> Optional[str]:
    """Replace the connection's secret; the old one stops working. None when not connected.

    Raises when the credentials can't be read or stored.
    """
    with webhook_secret_lock(user_id, provider):
        current = _read_creds_strict(user_id, provider)
        if not current:
            return None
        secret = generate_webhook_secret()
        _store(user_id, provider, {**current, SECRET_KEY: secret})
    logger.info("[WEBHOOK-AUTH] Rotated the webhook secret of a %s connection", sanitize(provider))
    return secret


def presented_secrets(req: Any) -> List[str]:
    """Every secret the caller sent, from all accepted locations."""
    candidates = [req.headers.get(WEBHOOK_HEADER_NAME, "").strip()]
    auth_header = req.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        candidates.append(auth_header[7:].strip())
    if req.authorization and req.authorization.type == "basic":
        candidates.append(req.authorization.password or "")
    candidates.append(req.args.get(WEBHOOK_QUERY_PARAM, "").strip())
    return [c for c in candidates if c]


def secret_matches(stored: Optional[str], presented: Optional[str]) -> bool:
    """Constant-time comparison; a missing value on either side never matches."""
    if not stored or not presented:
        return False
    return hmac.compare_digest(stored.encode("utf-8"), presented.encode("utf-8"))


def reject_unauthenticated_webhook(
    creds: Optional[Mapping[str, Any]], req: Any, *, provider_name: str, log_prefix: str, user_id: str
) -> Optional[Tuple[Response, int]]:
    """A 401 response when the request does not carry the connection's secret, else None."""
    stored = (creds or {}).get(SECRET_KEY)
    if not stored:
        logger.warning(
            "%s Webhook for user %s rejected: the connection has no webhook secret. "
            "Open the %s connector in Aurora and update the webhook URL configured in %s.",
            log_prefix, sanitize(user_id), provider_name, provider_name,
        )
        return jsonify({
            "error": f"Webhook secret not configured. Open the {provider_name} connector in Aurora "
                     "and copy the new webhook URL.",
        }), 401
    if not any(secret_matches(stored, candidate) for candidate in presented_secrets(req)):
        logger.warning("%s Invalid webhook secret for user %s", log_prefix, sanitize(user_id))
        return jsonify({"error": "Invalid webhook secret"}), 401
    return None


def webhook_url_with_secret(webhook_url: str, secret: str) -> str:
    """The URL to paste into vendors that cannot send a custom header."""
    return f"{webhook_url}?{urlencode({WEBHOOK_QUERY_PARAM: secret})}"


def redact_webhook_headers(headers: Any) -> Dict[str, str]:
    """Request headers safe to store with an alert: credentials and the webhook secret are redacted."""
    redacted = {}
    for key, value in headers.items():
        key_lower = key.lower()
        if key_lower in _SENSITIVE_HEADERS or "token" in key_lower or "secret" in key_lower:
            redacted[key] = "<REDACTED>"
        else:
            redacted[key] = value
    return redacted
