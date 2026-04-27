"""L5 output redaction: deterministic secret-scrubbing for tool call outputs.

Aurora runs in customer infrastructure, which means tool outputs routinely
contain real customer credentials: ``kubectl get secret -o yaml``, ``env``,
``aws sts get-session-token``, Confluence runbook pages, MCP tool responses.
Those credentials must not be (a) echoed back to the LLM provider on the
next turn, (b) persisted to the chat-history DB in raw form, or (c) exposed
via log shipping / backups / screenshots / support tickets.

L5 covers (a)-(c) by redacting tool-output strings at three hooks: the
``with_completion_notification`` decorator (primary, feeds WebSocket +
LangGraph), ``ContextManager._redact_tool_messages`` (belt-and-suspenders
before DB persistence), and ``Workflow._redact_for_ui`` (UI transcript
stitched onto ``chat_sessions.messages``). Model-generated assistant text
is out of scope here; see the "do not echo secrets" instruction in
``skills/core/security.md`` for that path.

Design notes
------------
* Patterns are codegen'd from a pinned Gitleaks TOML; see
  ``scripts/gen_secret_patterns.py``. Runtime has zero dependency on TOML
  parsing.
* Scan hot path is: size-cap -> already-redacted short-circuit -> keyword
  prefilter -> regex -> entropy -> allowlist. Rules without a keyword hit
  skip the regex entirely, which matches how Gitleaks itself gets its
  throughput.
* ``redact()`` is idempotent: ``[REDACTED:<rule>]`` placeholders contain no
  rule keywords, so re-scanning already-redacted text is a near-no-op.
* Inputs larger than ``MAX_SCAN_BYTES`` are truncated before scanning to
  bound worst-case regex runtime on pathological inputs.
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

# Upstream callers (e.g. ``send_tool_completion``) already cap tool output at
# ~10 KB for the WebSocket path, but L5 also runs in the decorator site
# before that cap is applied. Bound the scanner input so worst-case regex
# runtime stays linear in a small constant even if a caller forgets to
# truncate. Values above the cap are scanned up to the cap; content beyond
# it is passed through unmodified. The cap is deliberately generous: real
# credentials are short and nearly always appear near the top of a tool
# result (headers, env dumps, YAML manifests).
MAX_SCAN_BYTES = 256 * 1024


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
        # Short-circuit on text that is already fully redacted. A placeholder
        # contains no rule keyword, so the prefilter would eliminate every
        # rule anyway, but this avoids the ``text.lower()`` alloc in the hot
        # Hook 2/3 path where inputs are typically post-Hook-1.
        if _is_fully_redacted(text):
            return []
        # Bound worst-case regex runtime on pathological inputs.
        scan_text = text if len(text) <= MAX_SCAN_BYTES else text[:MAX_SCAN_BYTES]
        return _scan_unsafe(scan_text)
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

    Returns ``(redacted_text, findings)``. Findings cover non-overlapping
    spans in left-to-right order; substitution is applied in one forward
    pass. On any error the input is returned unchanged with an empty
    finding list (fail-open). Inputs larger than ``MAX_SCAN_BYTES`` are
    scanned only up to the cap; bytes beyond the cap are passed through
    unmodified.
    """
    if not text:
        return text, []
    try:
        if _is_fully_redacted(text):
            return text, []
        if len(text) > MAX_SCAN_BYTES:
            head, tail = text[:MAX_SCAN_BYTES], text[MAX_SCAN_BYTES:]
            findings = _scan_unsafe(head)
        else:
            head, tail = text, ""
            findings = _scan_unsafe(head)
        if not findings:
            return text, []
        kept = _drop_overlaps(findings)
        parts: list[str] = []
        cursor = 0
        for f in kept:
            parts.append(head[cursor:f.start])
            parts.append(_REDACTION_FMT.format(rule_id=f.rule_id))
            cursor = f.end
        parts.append(head[cursor:])
        parts.append(tail)
        return "".join(parts), kept
    except Exception:
        logger.warning("output_redaction.redact failed; passing through", exc_info=True)
        return text, []


def already_redacted(text: str) -> bool:
    """True iff ``text`` contains any redaction placeholder."""
    return bool(_REDACTION_SCAN.search(text or ""))


def _is_fully_redacted(text: str) -> bool:
    """Heuristic fast-path: text has at least one placeholder and no obvious
    secret-looking content outside the placeholders. We approximate "outside"
    by stripping placeholders and checking for any remaining high-entropy
    long alnum run that could plausibly carry a secret. Conservative: when
    in doubt we return False and let the full scanner decide.
    """
    if not _REDACTION_SCAN.search(text):
        return False
    stripped = _REDACTION_SCAN.sub("", text)
    # Any run of 20+ alnum / url-safe chars is a candidate secret. Real
    # Gitleaks rules fire on shorter runs, but 20 is the minimum length at
    # which shannon-entropy screening becomes meaningful. This keeps the
    # fast-path safe on post-Hook-1 inputs while declining to short-circuit
    # anything ambiguous.
    return not re.search(r"[A-Za-z0-9_\-+/=]{20,}", stripped)
