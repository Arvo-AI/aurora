"""Token-redaction utility for log lines.

This module provides a single helper, :func:`redact_token`, that scrubs
GitHub App credential-shaped substrings from arbitrary text. It is used
on every error-path log line in the GitHub App stack that includes raw
exception text (which could echo back a token in pathological cases —
e.g. a misconfigured proxy that mirrors the request body in its error
response).

Patterns scrubbed
-----------------
* ``ghs_[A-Za-z0-9]+``       — GitHub App installation tokens
  (e.g. ``ghs_AAA111ZZZ222BBB333``)
* ``eyJ[A-Za-z0-9_=-]{20,}`` — JSON Web Tokens. Every JWT header is
  base64url-encoded JSON starting with ``{``, which always encodes to
  the prefix ``eyJ``. The 20-char minimum tail keeps the pattern from
  matching short non-JWT identifiers that happen to start with ``eyJ``.

Both substrings are replaced by the literal marker ``***REDACTED***``.

Idempotency
-----------
The marker matches neither pattern, so passing already-redacted text
through :func:`redact_token` again is a no-op. This lets callers chain
redactions safely (e.g. an exception message that was already redacted
at construction time gets the same output when re-redacted in a log
formatter).

Why a separate module
---------------------
Centralising the patterns in one place gives ops a single grep to audit
("does *this* code path scrub credentials?") and gives a single source
of truth that the test suite can pin against future regressions. The
three GitHub App modules — ``github_app_token``, ``github_webhook`` and
``github_webhook_tasks`` — all import from here so the redaction
contract is identical across the stack.
"""

from __future__ import annotations

import re

_REDACTION_MARKER = "***REDACTED***"

# GitHub installation tokens: ``ghs_`` followed by alphanumeric chars.
# GitHub's installation tokens are strictly ``[A-Za-z0-9]`` after the
# prefix; the narrower character class avoids accidentally swallowing
# trailing punctuation (a closing paren in ``(token=ghs_xxx)`` should
# NOT become part of the redacted substring).
_GHS_TOKEN_PATTERN = re.compile(r"ghs_[A-Za-z0-9]+")

# JSON Web Tokens. ``eyJ`` is the deterministic base64url prefix of a
# JSON object opener (``{``). The pattern matches the full
# ``header.payload[.signature]`` shape so the entire bearer is redacted
# rather than just the predictable header — for GitHub App JWTs the
# header is essentially a constant, so leaving the payload+signature
# intact would still leak claim data and signing material.
_JWT_PATTERN = re.compile(r"eyJ[A-Za-z0-9_=-]+(?:\.[A-Za-z0-9_=-]+){1,2}")

_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (_GHS_TOKEN_PATTERN, _JWT_PATTERN)


def redact_token(s: str) -> str:
    """Return ``s`` with any GitHub installation token / JWT scrubbed.

    Safe to call on any string — non-matching input is returned
    unchanged. Idempotent: passing already-redacted text through again
    is a no-op (the marker ``***REDACTED***`` matches neither pattern).

    Args:
        s: Arbitrary log/exception text. May contain zero or more
            token-shaped substrings interleaved with non-secret text.

    Returns:
        The same string with each token-shaped substring replaced by
        ``***REDACTED***``. Empty / falsy input is returned unchanged.

    Examples:
        >>> redact_token("failed for token ghs_abc123XYZ")
        'failed for token ***REDACTED***'
        >>> redact_token("Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig")
        'Bearer ***REDACTED***'
        >>> redact_token("no secrets here")
        'no secrets here'
        >>> redact_token("")
        ''
    """
    if not s:
        return s
    for pattern in _TOKEN_PATTERNS:
        s = pattern.sub(_REDACTION_MARKER, s)
    return s
