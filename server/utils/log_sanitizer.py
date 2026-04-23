"""Sanitize user-controlled values before they reach log statements (S5145)."""

import hmac
import os
import re
from functools import lru_cache
from hashlib import sha256

from utils.providers import KNOWN_PROVIDERS

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

# Dict-based allowlist so static analyzers can see the return value is either a
# frozen literal from the set or the sentinel ``"unknown"`` — never the caller's
# input. This breaks taint flow more unambiguously than a conditional return.
_SAFE_PROVIDER_LABELS = {name: name for name in KNOWN_PROVIDERS}


@lru_cache(maxsize=1)
def _get_log_hash_salt() -> bytes:
    """Lazily resolve the HMAC key from ``FLASK_SECRET_KEY``.

    Evaluated on first call (not at import) so test collection and non-web
    entry points that import this module without Flask env vars don't die at
    import time.
    """
    key = os.environ.get("FLASK_SECRET_KEY")
    if not key:
        raise RuntimeError("FLASK_SECRET_KEY is required for log-hash correlation")
    return key.encode("utf-8")


def sanitize(value: object) -> str:
    """Strip control characters that could be used for log injection."""
    return _CONTROL_CHARS.sub("", str(value))


def safe_provider(value: object) -> str:
    """Return an allowlisted provider label safe for logging.

    The lookup goes through a dict whose keys are the literal set of known
    providers, so the function either returns one of those literals or the
    sentinel ``"unknown"`` — never the caller's raw input.
    """
    if not value:
        return "unknown"
    normalized = _CONTROL_CHARS.sub("", str(value)).strip().lower()
    return _SAFE_PROVIDER_LABELS.get(normalized, "unknown")


def hash_for_log(value: object, length: int = 12) -> str:
    """Return a non-reversible HMAC-SHA256 fingerprint for log correlation.

    The default ``length=12`` hex chars (~48 bits) is chosen for correlation
    only — it's NOT collision-resistant enough for de-duplication or any
    security-sensitive uniqueness check. Use for identifiers (user_id,
    account_id, credential prefixes) we want to correlate across logs without
    exposing raw values.
    """
    if value is None or value == "":
        return "-"
    text = _CONTROL_CHARS.sub("", str(value)).encode("utf-8")
    return hmac.new(_get_log_hash_salt(), text, sha256).hexdigest()[:length]
