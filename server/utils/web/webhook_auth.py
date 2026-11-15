"""Per-connection shared secret for inbound alert webhooks.

Alert webhook URLs look like ``/<vendor>/webhook/<user_id>``. The ``user_id``
only says which connection an alert belongs to: every member of an org can read
every other member's id from ``GET /api/orgs/current``, so it is not a
credential. The sender is authenticated by the ``webhook_secret`` stored with
the connector's credentials, compared in constant time.

Accepted locations, in order: the ``X-Aurora-Webhook-Secret`` header, an
``Authorization: Bearer`` token, the HTTP Basic password, or a ``?secret=``
query parameter for alerting tools that can only be given a URL.
"""

import hmac
import logging
import secrets
from typing import Any, Dict, Mapping, Optional, Tuple
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


def generate_webhook_secret() -> str:
    return secrets.token_urlsafe(32)


def with_webhook_secret(token_payload: Dict[str, Any], existing: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Return ``token_payload`` carrying the stored secret, or a new one.

    A reconnect or token rotation keeps the secret, so the webhook already
    configured in the vendor keeps working.
    """
    secret = (existing or {}).get(SECRET_KEY) or generate_webhook_secret()
    return {**token_payload, SECRET_KEY: secret}


def ensure_webhook_secret(user_id: str, provider: str, creds: Mapping[str, Any]) -> Optional[str]:
    """Return the connection's secret, adding one to a connection saved before secrets existed.

    Returns None when the secret could not be stored.
    """
    secret = creds.get(SECRET_KEY)
    if secret:
        return secret
    from utils.auth.token_management import store_tokens_in_db
    from utils.secrets.secret_ref_utils import get_token_owner_id

    payload = with_webhook_secret(dict(creds), None)
    try:
        # Org-shared credentials are written back under the row's owner, as token refresh does.
        store_tokens_in_db(get_token_owner_id(user_id, provider), payload, provider, schedule_prediscovery=False)
    except Exception:
        logger.exception("[WEBHOOK-AUTH] Failed to store a webhook secret for provider %s", sanitize(provider))
        return None
    logger.info("[WEBHOOK-AUTH] Added a webhook secret to an existing %s connection", sanitize(provider))
    return payload[SECRET_KEY]


def extract_presented_secret(req: Any) -> Optional[str]:
    """The secret the caller sent, from the first location that carries one."""
    header_secret = req.headers.get(WEBHOOK_HEADER_NAME)
    if header_secret:
        return header_secret.strip()
    auth_header = req.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip() or None
    if req.authorization and req.authorization.type == "basic" and req.authorization.password:
        return req.authorization.password
    query_secret = req.args.get(WEBHOOK_QUERY_PARAM)
    if query_secret:
        return query_secret.strip()
    return None


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
    if not secret_matches(stored, extract_presented_secret(req)):
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
