"""Shared-secret authentication for the inbound ServiceNow webhook. Kept free
of Flask so it can be unit tested and reused by the routes."""

import hmac
import secrets as pysecrets
from typing import List, Optional

WEBHOOK_SECRET_HEADER = "X-Aurora-Webhook-Secret"


def new_webhook_secret() -> str:
    return pysecrets.token_urlsafe(32)


def secret_matches(stored: Optional[str], header_value: Optional[str], authorization: Optional[str]) -> bool:
    """Constant-time check of the shared secret sent by the Business Rule.

    Accepted either in the X-Aurora-Webhook-Secret header or as a bearer token.
    """
    if not stored:
        return False
    candidates: List[str] = []
    if header_value:
        candidates.append(header_value.strip())
    if authorization and authorization.lower().startswith("bearer "):
        candidates.append(authorization[7:].strip())
    return any(hmac.compare_digest(stored, candidate) for candidate in candidates if candidate)
