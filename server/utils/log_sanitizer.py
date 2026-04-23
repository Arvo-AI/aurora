"""Sanitize user-controlled values before they reach log statements (S5145)."""

import hashlib
import re

from utils.providers import KNOWN_PROVIDERS

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def sanitize(value: object) -> str:
    """Strip control characters that could be used for log injection."""
    return _CONTROL_CHARS.sub("", str(value))


def safe_provider(value: object) -> str:
    """Return an allowlisted provider label safe for logging.

    CodeQL flags direct logging of provider names as clear-text sensitive data
    because upstream callers can receive untrusted input. Returning only a
    literal from the known set (or ``"unknown"``) eliminates that taint.
    """
    if not value:
        return "unknown"
    normalized = _CONTROL_CHARS.sub("", str(value)).strip().lower()
    return normalized if normalized in KNOWN_PROVIDERS else "unknown"


def hash_for_log(value: object, length: int = 12) -> str:
    """Return a non-reversible SHA-256 fingerprint for log correlation.

    Use for identifiers (user_id, account_id, credential prefixes) that we
    still want to correlate across logs but that shouldn't appear in clear
    text.
    """
    if value is None or value == "":
        return "-"
    text = _CONTROL_CHARS.sub("", str(value))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]
