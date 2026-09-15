"""Hidden markers that identify Aurora's own change-gating output.

Provider-neutral: both the GitHub and Bitbucket adapters embed these
markers in review/comment bodies so a later run can find its own prior
output without a provider-side bot-identity lookup. Two marker families:

- **Review-body marker** (``aurora-change-gating``): appended to every
  top-level review body. Carries the reviewed ``head_sha`` and a trimmed
  findings list (base64 JSON) used as re-review context.
- **Inline-finding marker** (``aurora-finding``): appended to every inline
  comment. Carries the finding's stable fingerprint so a re-review posts
  only net-new findings and keeps the rest in place.

The payload is base64 (not raw JSON) because findings text could contain
``--``, which terminates HTML comments.

IMPORTANT: a marker alone must never be treated as proof of authorship —
a human can paste one. Each provider pairs the marker with its own
author check (GitHub: ``user.type == "Bot"``; Bitbucket: author UUID in
the allowlisted ``{bot_uuid, token_owner_uuid}`` set).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Review-body marker
# ---------------------------------------------------------------------------

_MARKER_PREFIX = "aurora-change-gating"
_MARKER_VERSION = 1
# v1-strict: only payloads this code knows how to interpret.
_MARKER_RE = re.compile(rf"<!-- {_MARKER_PREFIX}:v{_MARKER_VERSION} ([A-Za-z0-9+/=]+) -->")
# Any-version: identifies a review as Aurora's even when the payload
# format is newer than this code (mixed-version fleet / rollback).
_MARKER_ANY_VERSION_RE = re.compile(rf"<!-- {_MARKER_PREFIX}:v\d+ [A-Za-z0-9+/=]+ -->")

# Transient "Aurora is reviewing…" progress-comment marker (debugging aid).
PROGRESS_MARKER = "<!-- aurora-change-gating:progress -->"


def encode_marker(findings: List[Dict[str, Any]], head_sha: str) -> str:
    """Encode findings + head SHA into a hidden HTML-comment marker."""
    payload = {"v": _MARKER_VERSION, "head_sha": head_sha, "findings": findings}
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    return f"<!-- {_MARKER_PREFIX}:v{_MARKER_VERSION} {encoded} -->"


def has_aurora_marker(body: Optional[str]) -> bool:
    """True when the body carries an Aurora marker of ANY version."""
    return bool(body) and _MARKER_ANY_VERSION_RE.search(body) is not None


def decode_marker(body: Optional[str]) -> Optional[Dict[str, Any]]:
    """Extract and decode the Aurora v1 marker from a review body.

    Returns the decoded dict (keys ``head_sha``, ``findings``) or None on
    any failure — missing/newer-version marker, bad base64, bad JSON,
    non-dict payload.
    """
    if not body:
        return None
    match = _MARKER_RE.search(body)
    if not match:
        return None
    try:
        decoded = json.loads(base64.b64decode(match.group(1)).decode("utf-8"))
    except ValueError:
        # binascii.Error, UnicodeDecodeError and JSONDecodeError all subclass
        # ValueError, so this catches bad base64, bad UTF-8 and bad JSON alike.
        return None
    if not isinstance(decoded, dict):
        return None
    return decoded


# ---------------------------------------------------------------------------
# Inline-finding marker
# ---------------------------------------------------------------------------

_INLINE_MARKER_PREFIX = "aurora-finding"
_INLINE_MARKER_RE = re.compile(rf"<!-- {_INLINE_MARKER_PREFIX}:([0-9a-f]+) -->")
_WHITESPACE_RE = re.compile(r"\s+")


def finding_fingerprint(finding: Dict[str, Any]) -> str:
    """Stable identity for a finding across re-reviews.

    Keyed on file path + a case/whitespace-normalized title so the SAME
    underlying issue keeps the SAME id even as line numbers shift between
    commits (line is deliberately excluded). Distinct titles in one file
    stay distinct. A materially reworded title yields a new id — the old
    comment is then treated as resolved and the new one posted, acceptable
    churn for that rare case.
    """
    path = str(finding.get("file_path") or "")
    title = _WHITESPACE_RE.sub(" ", str(finding.get("title") or "").strip().lower())
    return hashlib.sha256(f"{path}\n{title}".encode("utf-8")).hexdigest()[:16]


def inline_marker(finding: Dict[str, Any]) -> str:
    """The hidden fingerprint marker appended to an inline comment body."""
    return f"<!-- {_INLINE_MARKER_PREFIX}:{finding_fingerprint(finding)} -->"


def extract_inline_fingerprint(body: Optional[str]) -> Optional[str]:
    """Return the finding fingerprint embedded in an inline comment body, if any.

    Reads the LAST marker, not the first: ``render_inline_comment`` always
    appends the genuine marker at the very end, so a marker-shaped string
    inside the finding's ``explanation`` (e.g. when reviewing a diff that
    itself contains an ``aurora-finding`` marker) cannot shadow it. None for
    comments without any marker (human comments, or pre-fingerprint ones).
    """
    if not body:
        return None
    matches = _INLINE_MARKER_RE.findall(body)
    return matches[-1] if matches else None
