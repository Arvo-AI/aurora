"""Shared HMAC-SHA256 webhook signature utilities.

Used by Jenkins and CloudBees CI webhook receivers to validate
the ``X-Aurora-Signature`` header sent from Jenkinsfile snippets.
"""

import hashlib
import hmac

SIGNATURE_HEADER = "X-Aurora-Signature"


def verify_webhook_signature(payload_bytes: bytes, signature: str, secret: str) -> bool:
    """Validate an HMAC-SHA256 signature against the raw request body."""
    expected = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def verify_github_signature(payload_bytes: bytes, signature_header: str, secret: str) -> bool:
    """Validate GitHub's ``X-Hub-Signature-256`` header (``sha256=<hex>``)."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header[7:])
