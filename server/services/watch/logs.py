"""Turn raw container log lines into stable templates.

A template is the line with everything that varies between two occurrences of
"the same" message masked out (timestamps, IPs, ids, counters), so a baseline
can be learned per workload and a line that has never been seen before can be
recognised. HTTP status codes are kept as a class (status_ok,
status_server_error, ...) because they are the signal: a proxy that only ever
logged 200s and now logs 502s has a new template, while "batch of 500 items"
must not be mistaken for a 5xx.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

# Leading timestamps added by `kubectl logs --timestamps` or by the app itself.
_LEADING_TS = re.compile(
    r"^\s*(?:"
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"  # ISO 8601
    r"|\d{1,2}:[A-Z] \d{2} [A-Z][a-z]{2} \d{4} \d{2}:\d{2}:\d{2}\.\d+"  # redis "1:C 02 Oct 2026 ..."
    r"|[A-Z][a-z]{2} +\d{1,2} \d{2}:\d{2}:\d{2}"  # syslog "Oct  2 20:24:11"
    r"|\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}"  # nginx error log "2026/10/02 20:24:11"
    r")\s*"
)

# HTTP status in an unmistakably HTTP context. Exactly one named group matches.
_HTTP_STATUS = re.compile(
    r'(?:HTTP/\d(?:\.\d)?"\s+(?P<a>[1-5]\d\d)\b)'  # access log: "GET / HTTP/1.1" 502
    r"|(?:->\s*(?P<b>[1-5]\d\d)\b)"  # client log: GET /api/items -> 502
    r"|(?:\b(?:status|status_code|http_status|code)[=: ]+\"?(?P<c>[1-5]\d\d)\b)"
)

# Digit-free stand-ins so the number mask below cannot erase the status class.
_CLASS_TOKEN = {
    "1": "status_info",
    "2": "status_ok",
    "3": "status_redirect",
    "4": "status_client_error",
    "5": "status_server_error",
}

_MASKS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\[\d{2}/[A-Z][a-z]{2}/\d{4}:\d{2}:\d{2}:\d{2} [+-]\d{4}\]"), "[<ts>]"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "<ts>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"\b(?:[0-9a-f]{1,4}:){2,7}[0-9a-f]{1,4}\b", re.I), "<ip>"),
    (re.compile(r"\b(?:containerd|docker|cri-o)://[0-9a-f]+\b"), "<container-id>"),
    (re.compile(r"\b[0-9a-f]{12,}\b", re.I), "<hex>"),
    # Generated pod names: api-f64486c4c-cjj7k -> api-<pod>
    (re.compile(r"\b([a-z0-9]+(?:-[a-z0-9]+)*?)-[a-z0-9]{8,10}-[a-z0-9]{5}\b"), r"\1-<pod>"),
    (re.compile(r"\b\d+(?:\.\d+)?(?:ms|us|µs|ns|s|m|h)\b"), "<dur>"),
    (re.compile(r"\b\d+(?:\.\d+)?\s?(?:[KMGT]i?B|[KMGT]i|bytes)\b"), "<size>"),
    (re.compile(r"\d+"), "<n>"),
)

_WS = re.compile(r"\s+")

# Words that mark a line as reporting a failure, matched whole-word on the
# masked line. "warn"/"warning" are deliberately absent: a new deprecation
# warning is not an incident.
_ERROR_WORDS = re.compile(
    r"\b(?:error|errors|err|exception|traceback|fail|failed|failing|failure|fatal|panic|"
    r"refused|timeout|timed out|unreachable|unavailable|denied|forbidden|"
    r"cannot|can't|unable|emerg|crit|critical|killed|oom|giving up|"
    r"no such host|does not resolve|not resolve|name or service not known|"
    r"connection reset|broken pipe|segfault|unhandled)\b",
    re.I,
)

# A client or proxy reporting that a request did not get a good answer.
_REQUEST_FAILURE = re.compile(
    r"(?:->\s*(?:timeout|error|failed|refused))|upstream|while connecting to|"
    r"no live upstreams|connection refused|timed out",
    re.I,
)

MAX_TEMPLATE_CHARS = 240
MAX_SAMPLE_CHARS = 300
# Lines are cut before any regex runs: a workload controls its log lines, and an
# enormous one must not make templating expensive.
MAX_LINE_CHARS = 2000


@dataclass(frozen=True)
class LogTemplate:
    """One normalised log line."""

    template: str
    fingerprint: str
    is_error: bool
    http_class: str | None  # "5xx", "4xx", ... when the line carries an HTTP status
    is_request_failure: bool  # a request failed: 5xx, or a client/proxy timeout or refusal


def _replace_status(match: re.Match[str]) -> str:
    group = next(name for name in ("a", "b", "c") if match.group(name))
    prefix = match.group(0)[: match.start(group) - match.start(0)]
    return prefix + _CLASS_TOKEN[match.group(group)[0]]


def normalize(line: str) -> LogTemplate | None:
    """Template one raw log line; ``None`` for a blank line."""
    raw = (line or "")[:MAX_LINE_CHARS].strip()
    if not raw:
        return None
    raw = _LEADING_TS.sub("", raw, count=1)
    status = _HTTP_STATUS.search(raw)
    http_class = None
    text = raw
    if status:
        code = status.group("a") or status.group("b") or status.group("c")
        http_class = f"{code[0]}xx"
        text = _HTTP_STATUS.sub(_replace_status, raw, count=1)
    for pattern, replacement in _MASKS:
        text = pattern.sub(replacement, text)
    text = _WS.sub(" ", text).strip()[:MAX_TEMPLATE_CHARS]
    is_error = bool(_ERROR_WORDS.search(text)) or http_class == "5xx"
    is_request_failure = http_class == "5xx" or (is_error and bool(_REQUEST_FAILURE.search(text)))
    fingerprint = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
    return LogTemplate(text, fingerprint, is_error, http_class, is_request_failure)
