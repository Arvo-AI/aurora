"""Search the agent's tool calls (``execution_steps``) for the monitor page.

One implementation serves both the ``/api/monitor/tool-calls`` route and the
agent's ``search_tool_calls`` introspection tool, so the page and the chat answer
"which tool calls failed last week, and why" identically.

Everything returned is shaped and redacted here: callers never see raw rows,
and tool inputs/errors pass through the secret scrubber before leaving, since
a command line or an error body can carry a credential.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from chat.backend.agent.utils.tool_call_history import derive_command
from utils.query_helpers import clamp_int, fetch_dicts, iso_utc
from utils.security.output_redaction import redact
from utils.text.text_utils import truncate

# Period → PostgreSQL interval. "7d" is what "last week" means on the page.
PERIOD_INTERVALS = {
    "24h": "24 hours",
    "7d": "7 days",
    "30d": "30 days",
    "90d": "90 days",
}
DEFAULT_PERIOD = "7d"

# UI status names → execution_steps.status values.
STATUS_VALUES = {
    "failed": "error",
    "succeeded": "success",
    "running": "running",
}

SOURCES = ("chat", "incident")

MAX_PER_PAGE = 100
DEFAULT_PER_PAGE = 25
INPUT_SUMMARY_MAX_CHARS = 500
ERROR_MAX_CHARS = 2000
REASON_MAX_CHARS = 300
QUERY_MAX_CHARS = 200
TOP_TOOLS_LIMIT = 50

# Credentials passed as CLI flags or key=value pairs. The gitleaks-based
# ``redact`` catches token *shapes*; this catches a plain password that only
# its flag name gives away (``--password hunter2``).
_SECRET_FLAG_RE = re.compile(
    r"(?i)(--?(?:password|passwd|pwd|token|secret|api[-_]?key|access[-_]?key|"
    r"secret[-_]?key|client[-_]?secret|auth)(?:[= ]+|=))(\"[^\"]*\"|'[^']*'|\S+)"
)
_SECRET_KV_RE = re.compile(
    r"(?i)(\"?(?:password|passwd|token|secret|api[-_]?key|access[-_]?key|"
    r"client[-_]?secret|authorization)\"?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,}]+)"
)


# Fixed, user-facing messages for each invalid filter. Responses look these up
# by code rather than echoing the exception, so no exception text reaches a client.
FILTER_ERRORS = {
    "status": "status must be one of: failed, succeeded, running, all",
    "period": f"period must be one of: {', '.join(PERIOD_INTERVALS)}",
    "range": "since must be before until",
    "source": "source must be one of: chat, incident, all",
    "since": "since must be an ISO-8601 timestamp",
    "until": "until must be an ISO-8601 timestamp",
}


class InvalidFilter(ValueError):
    """A filter value the caller can fix; ``code`` is a key of FILTER_ERRORS."""

    def __init__(self, code: str):
        super().__init__(FILTER_ERRORS[code])
        self.code = code


def filter_error_message(exc: InvalidFilter) -> str:
    """The constant message for an invalid filter, safe to return to a caller."""
    return FILTER_ERRORS.get(getattr(exc, "code", ""), "Invalid filter")


@dataclass
class ToolCallFilters:
    status: Optional[str] = None      # failed | succeeded | running | None (all)
    period: Optional[str] = DEFAULT_PERIOD
    since: Optional[datetime] = None  # overrides period when set
    until: Optional[datetime] = None
    tool: Optional[str] = None
    query: Optional[str] = None
    source: Optional[str] = None      # chat | incident | None (both)
    page: int = 1
    per_page: int = DEFAULT_PER_PAGE

    @classmethod
    def parse(cls, raw: dict) -> "ToolCallFilters":
        """Validate loosely-typed input (query string or tool args)."""
        status = (raw.get("status") or "").strip().lower() or None
        if status in ("all",):
            status = None
        if status and status not in STATUS_VALUES:
            raise InvalidFilter("status")

        period = (raw.get("period") or "").strip().lower() or DEFAULT_PERIOD
        if period not in PERIOD_INTERVALS:
            raise InvalidFilter("period")

        since = _parse_ts(raw.get("since"), "since")
        until = _parse_ts(raw.get("until"), "until")
        if since and until and since > until:
            raise InvalidFilter("range")

        source = (raw.get("source") or "").strip().lower() or None
        if source in ("all",):
            source = None
        if source and source not in SOURCES:
            raise InvalidFilter("source")

        tool = (raw.get("tool") or "").strip() or None
        query = (raw.get("q") or raw.get("query") or "").strip() or None
        if query:
            query = query[:QUERY_MAX_CHARS]

        return cls(
            status=status,
            period=period,
            since=since,
            until=until,
            tool=tool,
            query=query,
            source=source,
            page=clamp_int(raw.get("page"), 1, 1, 10_000),
            per_page=clamp_int(raw.get("per_page"), DEFAULT_PER_PAGE, 1, MAX_PER_PAGE),
        )


def _parse_ts(value, field: str) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        raise InvalidFilter(field) from None


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _where(filters: ToolCallFilters, org_id: str, *, skip: tuple[str, ...] = ()) -> tuple[str, list]:
    """Build the WHERE clause; ``skip`` drops filters for facet queries."""
    clauses = ["es.org_id = %s"]
    params: list = [org_id]

    if filters.since or filters.until:
        if filters.since:
            clauses.append("es.started_at >= %s")
            params.append(filters.since)
        if filters.until:
            clauses.append("es.started_at < %s")
            params.append(filters.until)
    else:
        clauses.append("es.started_at >= NOW() - %s::interval")
        params.append(PERIOD_INTERVALS[filters.period or DEFAULT_PERIOD])

    if filters.status and "status" not in skip:
        clauses.append("es.status = %s")
        params.append(STATUS_VALUES[filters.status])
    if filters.tool and "tool" not in skip:
        clauses.append("es.tool_name = %s")
        params.append(filters.tool)
    if filters.source == "chat":
        clauses.append("es.incident_id IS NULL")
    elif filters.source == "incident":
        clauses.append("es.incident_id IS NOT NULL")
    if filters.query:
        like = f"%{_escape_like(filters.query)}%"
        clauses.append(
            "(es.tool_name ILIKE %s OR es.tool_input::text ILIKE %s "
            "OR es.error_message ILIKE %s)"
        )
        params.extend([like, like, like])

    return " AND ".join(clauses), params


# Sub-agent sessions are "<parent>::sa_N"; the chat a user can open is the parent.
_ROOT_SESSION_SQL = "split_part(es.session_id, '::', 1)"

_PAGE_SQL = f"""
    SELECT es.id, es.tool_name, es.tool_input, es.status, es.error_message,
           es.started_at, es.completed_at, es.duration_ms,
           es.session_id, es.incident_id,
           {_ROOT_SESSION_SQL} AS root_session_id,
           i.alert_title AS incident_title,
           cs.title AS chat_title
    FROM execution_steps es
    LEFT JOIN incidents i ON i.id = es.incident_id
    LEFT JOIN chat_sessions cs ON cs.id = {_ROOT_SESSION_SQL}
    WHERE {{where}}
    ORDER BY es.started_at DESC, es.id DESC
    LIMIT %s OFFSET %s
"""

_COUNT_SQL = "SELECT COUNT(*) FROM execution_steps es WHERE {where}"

_STATUS_COUNTS_SQL = """
    SELECT es.status, COUNT(*) FROM execution_steps es
    WHERE {where} GROUP BY es.status
"""

_TOOL_COUNTS_SQL = """
    SELECT es.tool_name, COUNT(*) AS total,
           COUNT(*) FILTER (WHERE es.status = 'error') AS failed
    FROM execution_steps es
    WHERE {where}
    GROUP BY es.tool_name
    ORDER BY failed DESC, total DESC, es.tool_name
    LIMIT %s
"""


def scrub(text: Optional[str]) -> str:
    """Remove secrets from a command/input/error string before returning it."""
    if not text:
        return ""
    cleaned, _ = redact(text)
    cleaned = _SECRET_FLAG_RE.sub(lambda m: f"{m.group(1)}[REDACTED]", cleaned)
    cleaned = _SECRET_KV_RE.sub(lambda m: f"{m.group(1)}[REDACTED]", cleaned)
    return cleaned


def _input_summary(tool_input) -> str:
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except (ValueError, TypeError):
            return truncate(tool_input, INPUT_SUMMARY_MAX_CHARS)
    command = derive_command(tool_input, INPUT_SUMMARY_MAX_CHARS)
    if command:
        return command
    if isinstance(tool_input, dict) and tool_input:
        return truncate(json.dumps(tool_input, default=str, sort_keys=True), INPUT_SUMMARY_MAX_CHARS)
    if tool_input in (None, {}, ""):
        return ""
    return truncate(str(tool_input), INPUT_SUMMARY_MAX_CHARS)


def failure_reason(error: Optional[str]) -> str:
    """One-line reason from a stored error body.

    Tool errors are often JSON (``{"error": "...", ...}``); pull the message
    out rather than showing the envelope. Otherwise use the first non-blank line.
    """
    if not error:
        return ""
    text = error.strip()
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        for key in ("error", "message", "detail", "details", "stderr"):
            value = parsed.get(key)
            if isinstance(value, dict):
                value = value.get("message") or json.dumps(value, default=str)
            if isinstance(value, str) and value.strip():
                text = value.strip()
                break
        else:
            errors = parsed.get("errors")
            if isinstance(errors, list) and errors:
                text = str(errors[0])
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return truncate(first, REASON_MAX_CHARS)


def _source(row: dict) -> dict:
    if row.get("incident_id"):
        incident_id = str(row["incident_id"])
        return {
            "type": "incident",
            "id": incident_id,
            "title": row.get("incident_title") or "Incident",
            "url": f"/incidents/{incident_id}",
        }
    session_id = row.get("root_session_id") or row.get("session_id")
    return {
        "type": "chat",
        "id": session_id,
        "title": row.get("chat_title") or "Chat",
        "url": f"/chat?sessionId={session_id}" if session_id else None,
    }


def _shape(row: dict) -> dict:
    error = scrub(truncate(row.get("error_message"), ERROR_MAX_CHARS)) if row.get("error_message") else ""
    status = row.get("status")
    return {
        "id": row["id"],
        "tool_name": row.get("tool_name") or "unknown",
        "provider": (row["tool_input"].get("provider") if isinstance(row.get("tool_input"), dict) else None),
        "input_summary": scrub(_input_summary(row.get("tool_input"))),
        "status": {"error": "failed", "success": "succeeded"}.get(status, status),
        "reason": scrub(failure_reason(row.get("error_message"))) if status == "error" else "",
        "error": error if status == "error" else "",
        "started_at": iso_utc(row.get("started_at")),
        "completed_at": iso_utc(row.get("completed_at")),
        "duration_ms": row.get("duration_ms"),
        "source": _source(row),
    }


def search_tool_calls(cur, org_id: str, filters: ToolCallFilters) -> dict:
    """Run the search on a cursor whose RLS context is already set to ``org_id``.

    Returns the page of tool calls plus facets: per-status counts (all filters
    but status) and per-tool totals (all filters but status and tool), which the
    page uses for its summary cards and tool dropdown.
    """
    where, params = _where(filters, org_id)
    offset = (filters.page - 1) * filters.per_page

    cur.execute(_COUNT_SQL.format(where=where), tuple(params))
    total = cur.fetchone()[0] or 0

    cur.execute(_PAGE_SQL.format(where=where), tuple(params + [filters.per_page, offset]))
    items = [_shape(r) for r in fetch_dicts(cur)]

    s_where, s_params = _where(filters, org_id, skip=("status",))
    cur.execute(_STATUS_COUNTS_SQL.format(where=s_where), tuple(s_params))
    by_status = {status: count for status, count in cur.fetchall()}

    t_where, t_params = _where(filters, org_id, skip=("status", "tool"))
    cur.execute(_TOOL_COUNTS_SQL.format(where=t_where), tuple(t_params + [TOP_TOOLS_LIMIT]))
    tools = [
        {"tool_name": name, "total": total_calls, "failed": failed}
        for name, total_calls, failed in cur.fetchall()
    ]

    return {
        "items": items,
        "total": total,
        "page": filters.page,
        "per_page": filters.per_page,
        "total_pages": max(1, -(-total // filters.per_page)),
        "summary": {
            "total": sum(by_status.values()),
            "failed": by_status.get("error", 0),
            "succeeded": by_status.get("success", 0),
            "running": by_status.get("running", 0),
        },
        "tools": tools,
        "filters": {
            "status": filters.status or "all",
            "period": filters.period,
            "since": iso_utc(filters.since),
            "until": iso_utc(filters.until),
            "tool": filters.tool,
            "q": filters.query,
            "source": filters.source or "all",
        },
    }
