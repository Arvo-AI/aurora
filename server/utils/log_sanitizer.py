"""Sanitize user-controlled values before they reach log statements (S5145)."""

import hmac
import os
import re
from hashlib import sha256

from utils.providers import KNOWN_PROVIDERS

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

# Dict-based allowlist so static analyzers can see the return value is either a
# frozen literal from the set or the sentinel ``"unknown"`` — never the caller's
# input. This breaks taint flow more unambiguously than a conditional return.
_SAFE_PROVIDER_LABELS = {name: name for name in KNOWN_PROVIDERS}

# Derive the log-correlation HMAC key from FLASK_SECRET_KEY, which is required
# in every deployment. Failing loudly here is intentional — a missing key would
# silently weaken log fingerprints and there's no safe fallback.
_LOG_HASH_SALT = os.environ["FLASK_SECRET_KEY"].encode("utf-8")


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

    Use for identifiers (user_id, account_id, credential prefixes) we still
    want to correlate across logs but that shouldn't appear in clear text.
    Uses a keyed MAC so the hash output isn't vulnerable to a precomputed
    rainbow attack on the small identifier space.
    """
    if value is None or value == "":
        return "-"
    text = _CONTROL_CHARS.sub("", str(value)).encode("utf-8")
    return hmac.new(_LOG_HASH_SALT, text, sha256).hexdigest()[:length]
