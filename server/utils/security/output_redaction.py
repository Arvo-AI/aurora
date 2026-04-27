"""L5 output redaction: deterministic secret-scrubbing for tool call outputs.

Aurora runs in customer infrastructure, which means tool outputs routinely
contain real customer credentials: ``kubectl get secret -o yaml``, ``env``,
``aws sts get-session-token``, Confluence runbook pages, MCP tool responses.
Those credentials must not be (a) echoed back to the LLM provider on the
next turn, (b) persisted to the chat-history DB in raw form, or (c) exposed
via log shipping / backups / screenshots / support tickets.

L5 covers (a)-(c) by redacting tool-output strings at two belt-and-suspenders
hooks (``send_tool_completion`` and ``save_context_history``). Model-
generated assistant text is out of scope here; see the "do not echo secrets"
instruction in ``skills/core/security.md`` for that path.

Design notes
------------
* Patterns are codegen'd from a pinned Gitleaks TOML; see
  ``scripts/gen_secret_patterns.py``. Runtime has zero dependency on TOML
  parsing.
* Scan hot path is a keyword prefilter -> regex -> entropy -> allowlist
  pipeline. Rules without a keyword hit skip the regex entirely, which
  matches how Gitleaks itself gets its throughput.
* ``redact()`` is idempotent: ``[REDACTED:<rule>]`` placeholders contain no
  rule keywords, so re-scanning already-redacted text is a near-no-op. This
  is what makes Hook 2 cheap in the common case.
* Fail-open: on any unexpected exception the original text is returned and
  a warning is logged. A redaction bug must not break a chat session.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass

from utils.security._generated_patterns import (
    GLOBAL_ALLOWLIST_REGEXES,
    GLOBAL_STOPWORDS,
    RULES,
    Rule,
)

logger = logging.getLogger(__name__)

_REDACTION_FMT = "[REDACTED:{rule_id}]"
_REDACTION_SCAN = re.compile(r"\[REDACTED:[a-z0-9][a-z0-9-]{0,64}\]")


@dataclass(frozen=True)
class Finding:
    """A single redaction hit. ``value_hash`` is a truncated sha256 of the
    captured value; the raw value is never stored or logged.
    """
    rule_id: str
    start: int
    end: int
    value_hash: str


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts = Counter(s)
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _capture(match: re.Match[str]) -> tuple[str, int, int]:
    """Return (value, start, end) for the most specific capture.

    Gitleaks rules often wrap the secret in capture group 1 with surrounding
    boundary assertions; a few omit the group entirely and rely on the full
    match. Walk the groups in order and use the first non-empty one so the
    redaction spans only the secret, not its delimiters.
    """
    for i in range(1, (match.lastindex or 0) + 1):
        value = match.group(i)
        if value:
            return value, match.start(i), match.end(i)
    return match.group(0), match.start(), match.end()


def _is_allowed(value: str, line: str, rule: Rule) -> bool:
    lowered = value.lower()
    # Gitleaks semantics: stopwords are substring-in-secret checks, not exact.
    for sw in rule.stopwords:
        if sw in lowered:
            return True
    for sw in GLOBAL_STOPWORDS:
        if sw in lowered:
            return True
    for target, pattern in rule.allowlist:
        haystack = line if target == "line" else value
        if pattern.search(haystack):
            return True
    for pattern in GLOBAL_ALLOWLIST_REGEXES:
        if pattern.search(value):
            return True
    return False


def _line_for(text: str, start: int, end: int) -> str:
    lo = text.rfind("\n", 0, start) + 1
    hi = text.find("\n", end)
    return text[lo:] if hi == -1 else text[lo:hi]


def _scan_unsafe(text: str) -> list[Finding]:
    lowered = text.lower()
    seen: set[tuple[int, int]] = set()
    findings: list[Finding] = []
    for rule in RULES:
        if rule.keywords and not any(k in lowered for k in rule.keywords):
            continue
        for match in rule.regex.finditer(text):
            value, start, end = _capture(match)
            span = (start, end)
            if span in seen:
                continue
            if rule.entropy_min and _shannon_entropy(value) < rule.entropy_min:
                continue
            if _is_allowed(value, _line_for(text, start, end), rule):
                continue
            seen.add(span)
            findings.append(Finding(
                rule_id=rule.id,
                start=start,
                end=end,
                value_hash=hashlib.sha256(value.encode("utf-8")).hexdigest()[:16],
            ))
    findings.sort(key=lambda f: (f.start, f.end))
    return findings


def scan(text: str) -> list[Finding]:
    """Return non-overlapping findings sorted by position. Never raises."""
    if not text:
        return []
    try:
        return _scan_unsafe(text)
    except Exception:
        logger.warning("output_redaction.scan failed; returning empty", exc_info=True)
        return []


def _drop_overlaps(findings: list[Finding]) -> list[Finding]:
    result: list[Finding] = []
    last_end = -1
    for f in findings:
        if f.start < last_end:
            continue
        result.append(f)
        last_end = f.end
    return result


def redact(text: str) -> tuple[str, list[Finding]]:
    """Redact detected secrets in ``text``.

    Returns ``(redacted_text, findings)``. Findings are preserved in
    left-to-right order for audit logging; substitution is applied in
    reverse to keep offsets stable. On any error the input is returned
    unchanged with an empty finding list (fail-open).
    """
    if not text:
        return text, []
    try:
        findings = _scan_unsafe(text)
        if not findings:
            return text, []
        kept = _drop_overlaps(findings)
        parts: list[str] = []
        cursor = 0
        for f in kept:
            parts.append(text[cursor:f.start])
            parts.append(_REDACTION_FMT.format(rule_id=f.rule_id))
            cursor = f.end
        parts.append(text[cursor:])
        return "".join(parts), kept
    except Exception:
        logger.warning("output_redaction.redact failed; passing through", exc_info=True)
        return text, []


def already_redacted(text: str) -> bool:
    """True iff ``text`` contains a redaction placeholder. Useful for tests
    and for callers that want to short-circuit double work.
    """
    return bool(_REDACTION_SCAN.search(text or ""))
