"""Sanitize user-controlled values before they reach log statements (S5145)."""

import hmac
import os
import re
from functools import lru_cache
from hashlib import sha256

from utils.providers import KNOWN_PROVIDERS

# Strip ASCII C0/C1 control chars plus Unicode line/paragraph separators and
# zero-width chars that can still forge new log lines in Unicode-aware parsers.
# Written with \u escapes (not literal glyphs) so invisible chars can't silently
# disappear on edit in an IDE that doesn't render them.
_CONTROL_CHARS = re.compile(
    "["
    "\x00-\x1f\x7f"          # C0 + DEL
    "\u2028\u2029"            # LINE SEPARATOR, PARAGRAPH SEPARATOR
    "\u200b-\u200f"           # ZWSP, ZWNJ, ZWJ, LRM, RLM
    "\u2060\ufeff"            # WORD JOINER, ZERO WIDTH NO-BREAK SPACE / BOM
    "]"
)

# Dict-based allowlist so static analyzers can see the return value is either a
# frozen literal from the set or the sentinel ``"unknown"`` — never the caller's
# input. This breaks taint flow more unambiguously than a conditional return.
_SAFE_PROVIDER_LABELS = {name: name for name in KNOWN_PROVIDERS}

# Sentinel returned by hash_for_log when the HMAC key is unavailable. Logging
# must never crash a request, so callers just see a marker instead of a hash.
_MISSING_SALT_SENTINEL = "?"


@lru_cache(maxsize=1)
def _get_log_hash_salt() -> bytes | None:
    """Lazily resolve the HMAC key from ``FLASK_SECRET_KEY``.

    Returns ``None`` if the env var is missing so that ``hash_for_log`` can
    degrade to a sentinel rather than taking down any request whose only sin
    was logging a user_id. Cached so we don't re-read on every log line.
    """
    key = os.environ.get("FLASK_SECRET_KEY")
    if not key:
        return None
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

    Degrades to a sentinel (``"?"``) if ``FLASK_SECRET_KEY`` is unset so that a
    missing salt can never turn a successful request path into a 500.
    """
    if value is None or value == "":
        return "-"
    salt = _get_log_hash_salt()
    if salt is None:
        return _MISSING_SALT_SENTINEL
    text = _CONTROL_CHARS.sub("", str(value)).encode("utf-8")
    return hmac.new(salt, text, sha256).hexdigest()[:length]
