"""Org-wide chat audit: every chat, its failed tool calls, and the issues behind them.

One query layer serves two callers:

* the monitor page's Conversations tab (admins only): ``list_chats`` and
  ``get_transcript``;
* the agent's read-only ``search_tool_calls`` tool: ``search_failed_tool_calls``.

Both share the same scoping (every query filters on ``org_id``; callers also
set RLS), the same issue classification (``issue_classifier``), the same
failed-tool-call query (``_failed_tool_calls``) and the same redaction
(``scrub``). Nothing leaves this module unredacted: transcripts and tool
errors routinely carry credentials.

Full-text search uses a GIN index over the message texts
(``idx_chat_sessions_message_fts``, created in ``db_utils`` from
``SEARCH_INDEX_SQL``); ``SEARCH_DOC_SQL`` must stay identical to that index
expression or Postgres won't use it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional

from chat.backend.agent.utils.tool_call_history import derive_command
from services.monitor.issue_classifier import (
    CATEGORY_LABELS,
    ISSUE_CATEGORIES,
    OTHER,
    classify_reply,
    classify_tool_error,
    classify_tool_output,
    sql_category_case,
)
from utils.metrics_periods import PERIOD_MAP
from utils.query_helpers import clamp_int, fetch_dicts, iso_utc
from utils.security.output_redaction import redact
from utils.text.text_utils import truncate

# "7d" is what "last week" means on the page and in the agent tool.
PERIOD_INTERVALS = {"24h": "24 hours", **{p: PERIOD_MAP[p] for p in ("7d", "30d", "90d")}}
DEFAULT_PERIOD = "7d"

DEFAULT_PER_PAGE = 25
MAX_PER_PAGE = 100
QUERY_MAX_CHARS = 200
SNIPPET_RADIUS = 120
USERS_FACET_LIMIT = 200
FAILURES_PER_CHAT = 5
USER_ID_MAX_CHARS = 255
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_:-]{1,64}$")

INPUT_SUMMARY_MAX_CHARS = 500
REASON_MAX_CHARS = 300
# Transcript caps: one view of one chat, but RCA chats can hold megabytes.
MAX_TRANSCRIPT_MESSAGES = 2000
MESSAGE_TEXT_MAX_CHARS = 20_000
TOOL_IO_MAX_CHARS = 4_000

# ---------------------------------------------------------------------------
# Redaction and shaping
# ---------------------------------------------------------------------------

# ``redact`` (gitleaks rules) catches token *shapes*; these catch a plaintext
# secret that only its flag or key name gives away (``--password hunter2``).
_SECRET_NAMES = (
    r"password|passwd|pwd|token|secret|api[-_]?key|access[-_]?key|"
    r"secret[-_]?key|client[-_]?secret|authorization|auth"
)
_SECRET_FLAG_RE = re.compile(rf"(?i)(--?(?:{_SECRET_NAMES})(?:[= ]+))(\"[^\"]*\"|'[^']*'|\S+)")
_SECRET_KV_RE = re.compile(rf"(?i)(\"?(?:{_SECRET_NAMES})\"?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,}}]+)")
_SECRET_KEY_RE = re.compile(rf"(?i)^({_SECRET_NAMES})$")


def scrub(text: Optional[str]) -> str:
    """Remove secrets from free text (a command, an error, a message)."""
    if not text:
        return ""
    cleaned, _ = redact(text)
    cleaned = _SECRET_FLAG_RE.sub(lambda m: f"{m.group(1)}[REDACTED]", cleaned)
    return _SECRET_KV_RE.sub(lambda m: f"{m.group(1)}[REDACTED]", cleaned)


def _scrub_value(value):
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, list):
        return [_scrub_value(v) for v in value]
    if isinstance(value, dict):
        return {
            k: "[REDACTED]" if isinstance(k, str) and _SECRET_KEY_RE.match(k) else _scrub_value(v)
            for k, v in value.items()
        }
    return value


def scrub_json_text(text: Optional[str], limit: int = TOOL_IO_MAX_CHARS) -> str:
    """Scrub a tool input/output, keeping it valid JSON when it is JSON.

    The chat's tool widgets parse these strings per tool, so redaction must
    happen on the values, not on the serialized text.
    """
    if not text:
        return ""
    if not isinstance(text, str):
        text = json.dumps(text, default=str)
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return scrub(truncate(text, limit))
    if not isinstance(parsed, (dict, list)):
        return scrub(truncate(text, limit))
    return truncate(json.dumps(_scrub_value(parsed), default=str), limit)


def summarize_input(tool_input) -> str:
    """The command a tool ran, or a compact rendering of its arguments (unredacted)."""
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
    return "" if tool_input in (None, {}, "") else truncate(str(tool_input), INPUT_SUMMARY_MAX_CHARS)


def failure_reason(error: Optional[str]) -> str:
    """One-line reason from a stored error body (unwraps ``{"error": ...}`` envelopes)."""
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


def _snippet(text: str, query: str) -> str:
    """A redacted window of ``text`` around the first query term (or its start)."""
    terms = [t for t in re.findall(r"[\w.-]+", query.lower()) if len(t) > 1 and t not in ("or", "and")]
    lower = text.lower()
    pos = min((p for p in (lower.find(t) for t in terms) if p >= 0), default=0)
    start, end = max(0, pos - SNIPPET_RADIUS), min(len(text), pos + SNIPPET_RADIUS)
    snippet = text[start:end].replace("\n", " ").strip()
    return scrub(("…" if start else "") + snippet + ("…" if end < len(text) else ""))


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

SOURCE_LABELS = {
    "web": "Web chat",
    "slack": "Slack",
    "google_chat": "Google Chat",
    "teams": "Teams",
    "rca": "Incident RCA",
    "action": "Action",
    "scheduled": "Scheduled",
    "background": "Background",
}
STATUS_LABELS = {"completed": "Completed", "failed": "Failed", "in_progress": "In progress"}

# Constant, user-facing messages: responses look them up by code and never
# echo exception text.
FILTER_ERRORS = {
    "period": f"period must be one of: {', '.join(PERIOD_INTERVALS)}",
    "source": f"source must be one of: {', '.join(SOURCE_LABELS)}, all",
    "status": "status must be one of: completed, failed, in_progress, all",
    "issue": f"issue must be one of: {', '.join((*ISSUE_CATEGORIES, OTHER))}, any, all",
    "user": "user is not a valid user id",
    "session": "Invalid chat id",
}


class InvalidFilter(ValueError):
    def __init__(self, code: str):
        super().__init__(FILTER_ERRORS[code])
        self.code = code


def filter_error_message(exc: InvalidFilter) -> str:
    return FILTER_ERRORS.get(getattr(exc, "code", ""), "Invalid filter")


def _opt(raw: dict, key: str) -> Optional[str]:
    value = str(raw.get(key) or "").strip()
    return None if value.lower() in ("", "all") else value


def _parse_period(raw: dict) -> str:
    period = (_opt(raw, "period") or DEFAULT_PERIOD).lower()
    if period not in PERIOD_INTERVALS:
        raise InvalidFilter("period")
    return period


def _parse_issue(raw: dict) -> Optional[str]:
    issue = (_opt(raw, "issue") or "").lower() or None
    if issue and issue not in (*ISSUE_CATEGORIES, OTHER, "any"):
        raise InvalidFilter("issue")
    return issue


def _parse_query(raw: dict) -> Optional[str]:
    query = _opt(raw, "q")
    return query[:QUERY_MAX_CHARS] if query else None


@dataclass
class ChatFilters:
    period: str = DEFAULT_PERIOD
    source: Optional[str] = None
    status: Optional[str] = None
    issue: Optional[str] = None      # an issue category (incl. "other"), "any" failure, or None
    user_id: Optional[str] = None
    query: Optional[str] = None
    page: int = 1
    per_page: int = DEFAULT_PER_PAGE

    @classmethod
    def parse(cls, raw: dict) -> "ChatFilters":
        source = (_opt(raw, "source") or "").lower() or None
        if source and source not in SOURCE_LABELS:
            raise InvalidFilter("source")
        status = (_opt(raw, "status") or "").lower() or None
        if status and status not in STATUS_LABELS:
            raise InvalidFilter("status")
        user_id = _opt(raw, "user")
        if user_id and len(user_id) > USER_ID_MAX_CHARS:
            raise InvalidFilter("user")
        return cls(
            period=_parse_period(raw),
            source=source,
            status=status,
            issue=_parse_issue(raw),
            user_id=user_id,
            query=_parse_query(raw),
            page=clamp_int(raw.get("page"), 1, 1, 10_000),
            per_page=clamp_int(raw.get("per_page"), DEFAULT_PER_PAGE, 1, MAX_PER_PAGE),
        )


def validate_session_id(session_id: str) -> str:
    if not session_id or not SESSION_ID_RE.match(session_id):
        raise InvalidFilter("session")
    return session_id


# ---------------------------------------------------------------------------
# SQL building blocks
# ---------------------------------------------------------------------------

# trigger_metadata.source values from alert/incident integrations → an RCA.
_ALERT_SOURCES = (
    "grafana", "pagerduty", "datadog", "sentry", "opsgenie", "newrelic", "cloudwatch",
    "splunk", "netdata", "incidentio", "dynatrace", "bigpanda", "kibana", "servicenow",
    "jira", "spinnaker", "incident", "on_incident", "incident_chat", "stored_event",
)
_TRIGGER_SRC = "LOWER(COALESCE(cs.ui_state->'triggerMetadata'->>'source', ''))"
_ALERT_LIST = ", ".join(f"'{s}'" for s in _ALERT_SOURCES)  # constants, never input

SOURCE_SQL = f"""(CASE
    WHEN cs.incident_id IS NOT NULL THEN 'rca'
    WHEN {_TRIGGER_SRC} IN ('slack', 'slack_button') THEN 'slack'
    WHEN {_TRIGGER_SRC} IN ('google_chat', 'google_chat_button') THEN 'google_chat'
    WHEN {_TRIGGER_SRC} = 'teams' THEN 'teams'
    WHEN {_TRIGGER_SRC} = 'action' THEN 'action'
    WHEN {_TRIGGER_SRC} = 'schedule' THEN 'scheduled'
    WHEN {_TRIGGER_SRC} IN ({_ALERT_LIST}) THEN 'rca'
    WHEN {_TRIGGER_SRC} <> '' OR cs.ui_state->>'isBackground' = 'true' THEN 'background'
    ELSE 'web'
END)"""

STATUS_SQL = """(CASE
    WHEN cs.status = 'in_progress' THEN 'in_progress'
    WHEN cs.status IN ('failed', 'error') THEN 'failed'
    ELSE 'completed'
END)"""

_MESSAGES_ARRAY = "(CASE WHEN jsonb_typeof(cs.messages) = 'array' THEN cs.messages ELSE '[]'::jsonb END)"
_MESSAGE_COUNT = f"jsonb_array_length({_MESSAGES_ARRAY})"

# Must match idx_chat_sessions_message_fts exactly.
SEARCH_DOC_SQL = (
    "to_tsvector('simple'::regconfig, "
    "left((jsonb_path_query_array(cs.messages, '$[*].text'::jsonpath))::text, 200000))"
)
SEARCH_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_chat_sessions_message_fts ON chat_sessions USING GIN "
    "((to_tsvector('simple'::regconfig, "
    "left((jsonb_path_query_array(messages, '$[*].text'::jsonpath))::text, 200000))))"
)

# Chats and their sub-agents ("<chat>::sa_N") share a root session id.
ROOT_SESSION_SQL = "split_part(es.session_id, '::', 1)"


def _chat_where(f: ChatFilters, org_id: str, *, skip: tuple[str, ...] = ()) -> tuple[str, list]:
    """WHERE for chat_sessions ``cs``. ``skip`` drops filters for facet queries."""
    clauses = ["cs.org_id = %s", "cs.is_active = true", "cs.updated_at >= NOW() - %s::interval"]
    params: list = [org_id, PERIOD_INTERVALS[f.period]]
    if f.source and "source" not in skip:
        clauses.append(f"{SOURCE_SQL} = %s")
        params.append(f.source)
    if f.status and "status" not in skip:
        clauses.append(f"{STATUS_SQL} = %s")
        params.append(f.status)
    if f.user_id and "user" not in skip:
        clauses.append("cs.user_id = %s")
        params.append(f.user_id)
    if f.query and "query" not in skip:
        clauses.append(f"{SEARCH_DOC_SQL} @@ websearch_to_tsquery('simple', %s)")
        params.append(f.query)
    if f.issue and "issue" not in skip:
        exists = (
            "EXISTS (SELECT 1 FROM execution_steps es "
            f"WHERE es.org_id = cs.org_id AND {ROOT_SESSION_SQL} = cs.id AND es.status = 'error'"
        )
        if f.issue == "any":
            clauses.append(exists + ")")
        else:
            case_sql, case_params = sql_category_case("es.error_message")
            clauses.append(exists + f" AND {case_sql} = %s)")
            params.extend(case_params + [f.issue])
    return " AND ".join(clauses), params


# ---------------------------------------------------------------------------
# Failed tool calls (shared by the page and the agent tool)
# ---------------------------------------------------------------------------

def _failed_tool_calls(
    cur, org_id: str, *, chat_ids: Optional[list[str]] = None, period: Optional[str] = None,
    category: Optional[str] = None, tool: Optional[str] = None, query: Optional[str] = None,
    source: Optional[str] = None, per_chat: Optional[int] = None, limit: int = 50,
) -> list[dict]:
    """Failed ``execution_steps`` rows, classified, redacted and anchored to their message.

    ``message_index`` is the transcript message holding the call (None for
    sub-agent calls, which don't appear in the parent transcript).
    """
    case_sql, case_params = sql_category_case("es.error_message")
    clauses = ["es.org_id = %s", "es.status = 'error'"]
    params: list = [org_id]
    if chat_ids is not None:
        clauses.append(f"{ROOT_SESSION_SQL} = ANY(%s)")
        params.append(chat_ids)
    if period:
        clauses.append("es.started_at >= NOW() - %s::interval")
        params.append(PERIOD_INTERVALS[period])
    if category and category != "any":
        clauses.append(f"{case_sql} = %s")
        params.extend(case_params + [category])
    if tool:
        clauses.append("es.tool_name = %s")
        params.append(tool)
    if source == "chat":
        clauses.append("es.incident_id IS NULL")
    elif source == "incident":
        clauses.append("es.incident_id IS NOT NULL")
    if query:
        like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        clauses.append("(es.tool_name ILIKE %s OR es.tool_input::text ILIKE %s OR es.error_message ILIKE %s)")
        params.extend([like, like, like])
    # Cap per chat *before* the overall limit, so one noisy chat can't crowd out the rest.
    rank_filter = "ranked.rn <= %s" if per_chat else "TRUE"
    cur.execute(
        f"""SELECT f.*, (
                SELECT (e.idx - 1)::int
                FROM chat_sessions c,
                     jsonb_array_elements(CASE WHEN jsonb_typeof(c.messages) = 'array'
                                               THEN c.messages ELSE '[]'::jsonb END)
                         WITH ORDINALITY AS e(m, idx)
                WHERE c.id = f.chat_id AND c.org_id = %s AND f.tool_call_id IS NOT NULL
                  AND e.m->'toolCalls' @> jsonb_build_array(jsonb_build_object('id', f.tool_call_id))
                LIMIT 1
            ) AS message_index
            FROM (
                SELECT * FROM (
                    SELECT es.id, es.tool_name, es.tool_input, es.error_message, es.started_at,
                           es.tool_call_id, es.incident_id, {ROOT_SESSION_SQL} AS chat_id,
                           ROW_NUMBER() OVER (PARTITION BY {ROOT_SESSION_SQL}
                                              ORDER BY es.started_at DESC, es.id DESC) AS rn
                    FROM execution_steps es
                    WHERE {" AND ".join(clauses)}
                ) ranked
                WHERE {rank_filter}
                ORDER BY ranked.started_at DESC, ranked.id DESC
                LIMIT %s
            ) f
            ORDER BY f.started_at DESC, f.id DESC""",
        tuple([org_id] + params + ([per_chat] if per_chat else []) + [limit]),
    )
    return [
        {
            "chat_id": r["chat_id"],
            "incident_id": str(r["incident_id"]) if r.get("incident_id") else None,
            "message_index": r.get("message_index"),
            "tool_call_id": r.get("tool_call_id"),
            "tool_name": r.get("tool_name") or "unknown",
            "input": scrub(summarize_input(r.get("tool_input"))),
            "category": classify_tool_error(r.get("error_message")),
            "reason": scrub(failure_reason(r.get("error_message"))),
            "started_at": iso_utc(r.get("started_at")),
        }
        for r in fetch_dicts(cur)
    ]


def _issue_totals(cur, org_id: str, period: str, chat_where: str = "TRUE", chat_params=()) -> list[dict]:
    """Failed tool calls per issue category in ``period`` (optionally within chat filters)."""
    case_sql, case_params = sql_category_case("es.error_message")
    cur.execute(
        f"""SELECT {case_sql} AS category, COUNT(*), COUNT(DISTINCT {ROOT_SESSION_SQL})
            FROM execution_steps es
            LEFT JOIN chat_sessions cs ON cs.id = {ROOT_SESSION_SQL} AND cs.org_id = es.org_id
            WHERE es.org_id = %s AND es.status = 'error'
              AND es.started_at >= NOW() - %s::interval AND {chat_where}
            GROUP BY 1""",
        tuple(case_params + [org_id, PERIOD_INTERVALS[period]] + list(chat_params)),
    )
    found = {cat: (calls, chats) for cat, calls, chats in cur.fetchall()}
    return [
        {"category": cat, "label": CATEGORY_LABELS[cat],
         "calls": found.get(cat, (0, 0))[0], "chats": found.get(cat, (0, 0))[1]}
        for cat in (*ISSUE_CATEGORIES, OTHER)
    ]


def search_failed_tool_calls(cur, org_id: str, raw: dict, limit: int = 20) -> dict:
    """Agent tool entry point: failed tool calls with reasons, plus counts per issue."""
    period = _parse_period(raw)
    source = (_opt(raw, "source") or "").lower() or None
    if source and source not in ("chat", "incident"):
        raise InvalidFilter("source")
    calls = _failed_tool_calls(
        cur, org_id, period=period, category=_parse_issue(raw), tool=_opt(raw, "tool"),
        query=_parse_query(raw), source=source, limit=limit,
    )
    for c in calls:
        c["link"] = f"/incidents/{c['incident_id']}" if c["incident_id"] else f"/chat?sessionId={c['chat_id']}"
    return {"tool_calls": calls, "by_issue": _issue_totals(cur, org_id, period), "period": period}


# ---------------------------------------------------------------------------
# Conversations list
# ---------------------------------------------------------------------------

def _issue_counts_for(cur, org_id: str, chat_ids: list[str]) -> dict[str, dict[str, int]]:
    case_sql, case_params = sql_category_case("es.error_message")
    cur.execute(
        f"""SELECT {ROOT_SESSION_SQL}, {case_sql}, COUNT(*)
            FROM execution_steps es
            WHERE es.org_id = %s AND es.status = 'error' AND {ROOT_SESSION_SQL} = ANY(%s)
            GROUP BY 1, 2""",
        tuple(case_params + [org_id, chat_ids]),
    )
    out: dict[str, dict[str, int]] = {}
    for sid, category, count in cur.fetchall():
        out.setdefault(sid, {})[category] = count
    return out


def _snippets_for(cur, org_id: str, chat_ids: list[str], query: str) -> dict[str, dict]:
    """First matching message per chat (bounded to the page's chats)."""
    cur.execute(
        f"""SELECT cs.id, hit.msg_index, hit.sender, hit.text
            FROM chat_sessions cs
            JOIN LATERAL (
                SELECT (e.idx - 1)::int AS msg_index, e.m->>'sender' AS sender,
                       LEFT(e.m->>'text', %s) AS text
                FROM jsonb_array_elements({_MESSAGES_ARRAY}) WITH ORDINALITY AS e(m, idx)
                WHERE to_tsvector('simple', COALESCE(e.m->>'text', ''))
                      @@ websearch_to_tsquery('simple', %s)
                ORDER BY e.idx LIMIT 1
            ) hit ON true
            WHERE cs.org_id = %s AND cs.id = ANY(%s)""",
        (MESSAGE_TEXT_MAX_CHARS, query, org_id, chat_ids),
    )
    return {
        sid: {"message_index": idx, "sender": "user" if sender == "user" else "aurora",
              "text": _snippet(text or "", query)}
        for sid, idx, sender, text in cur.fetchall()
    }


_CHAT_COLUMNS = f"""cs.id, cs.title, cs.user_id, u.email AS user_email, u.name AS user_name,
    {SOURCE_SQL} AS source, {_TRIGGER_SRC} AS trigger_source, {STATUS_SQL} AS status,
    cs.created_at, cs.updated_at, cs.incident_id, {_MESSAGE_COUNT} AS message_count"""


def _chat_summary(r: dict) -> dict:
    return {
        "id": r["id"],
        "title": r.get("title") or "Untitled chat",
        "user": {"id": r.get("user_id"), "email": r.get("user_email"), "name": r.get("user_name")},
        "source": r["source"],
        "trigger_source": r.get("trigger_source") or None,
        "status": r["status"],
        "created_at": iso_utc(r.get("created_at")),
        "updated_at": iso_utc(r.get("updated_at")),
        "message_count": r.get("message_count") or 0,
        "incident_id": str(r["incident_id"]) if r.get("incident_id") else None,
    }


def list_chats(cur, org_id: str, f: ChatFilters) -> dict:
    """One page of the org's chats with issue badges and failures, plus facets and counts."""
    where, params = _chat_where(f, org_id)
    cur.execute(f"SELECT COUNT(*) FROM chat_sessions cs WHERE {where}", tuple(params))
    total = cur.fetchone()[0] or 0

    cur.execute(
        f"""SELECT {_CHAT_COLUMNS}
            FROM chat_sessions cs LEFT JOIN users u ON u.id = cs.user_id
            WHERE {where}
            ORDER BY cs.updated_at DESC, cs.id DESC
            LIMIT %s OFFSET %s""",
        tuple(params + [f.per_page, (f.page - 1) * f.per_page]),
    )
    rows = fetch_dicts(cur)
    ids = [r["id"] for r in rows]

    issues, failures, snippets = {}, {}, {}
    if ids:
        issues = _issue_counts_for(cur, org_id, ids)
        # Only fetch failure detail for chats that have failures.
        failing = [i for i in ids if issues.get(i)]
        if failing:
            for fail in _failed_tool_calls(
                cur, org_id, chat_ids=failing, category=f.issue, per_chat=FAILURES_PER_CHAT,
                limit=FAILURES_PER_CHAT * len(failing),
            ):
                failures.setdefault(fail["chat_id"], []).append(fail)
        if f.query:
            snippets = _snippets_for(cur, org_id, ids, f.query)

    chats = []
    for r in rows:
        by_cat = issues.get(r["id"], {})
        chats.append({
            **_chat_summary(r),
            "issues": {cat: n for cat, n in by_cat.items() if cat != OTHER},
            "failed_tool_calls": sum(by_cat.values()),
            "failures": failures.get(r["id"], []),
            "match": snippets.get(r["id"]),
        })

    counts_where, counts_params = _chat_where(f, org_id, skip=("status", "issue", "query"))
    return {
        "chats": chats,
        "total": total,
        "page": f.page,
        "per_page": f.per_page,
        "total_pages": max(1, -(-total // f.per_page)),
        "issue_counts": _issue_totals(cur, org_id, f.period, counts_where, counts_params),
        "sources": _source_facet(cur, org_id, f),
        "users": _user_facet(cur, org_id, f),
        "labels": {"sources": SOURCE_LABELS, "issues": CATEGORY_LABELS, "statuses": STATUS_LABELS},
    }


def _source_facet(cur, org_id: str, f: ChatFilters) -> list[dict]:
    where, params = _chat_where(f, org_id, skip=("source",))
    cur.execute(f"SELECT {SOURCE_SQL}, COUNT(*) FROM chat_sessions cs WHERE {where} GROUP BY 1", tuple(params))
    counts = dict(cur.fetchall())
    return [{"source": s, "label": label, "count": counts.get(s, 0)} for s, label in SOURCE_LABELS.items()]


def _user_facet(cur, org_id: str, f: ChatFilters) -> list[dict]:
    where, params = _chat_where(f, org_id, skip=("user",))
    cur.execute(
        f"""SELECT cs.user_id, MAX(u.email), MAX(u.name), COUNT(*)
            FROM chat_sessions cs LEFT JOIN users u ON u.id = cs.user_id
            WHERE {where}
            GROUP BY cs.user_id ORDER BY COUNT(*) DESC LIMIT %s""",
        tuple(params + [USERS_FACET_LIMIT]),
    )
    return [{"id": uid, "email": email, "name": name, "count": n} for uid, email, name, n in cur.fetchall()]


# ---------------------------------------------------------------------------
# Read-only transcript
# ---------------------------------------------------------------------------

def _transcript_tool_call(tc: dict, step: Optional[dict]) -> tuple[dict, Optional[str], str]:
    """A tool call in the chat UI's ``ToolCall`` shape, plus its issue and reason."""
    output = tc.get("output")
    if output is not None and not isinstance(output, str):
        output = json.dumps(output, default=str)
    step_failed = bool(step and step.get("status") == "error")
    error_text = (step or {}).get("error_message") or output or ""
    issue = classify_tool_error(error_text) if step_failed else classify_tool_output(output)
    failed = step_failed or issue is not None
    reason = scrub(failure_reason(error_text)) if failed else ""
    shaped = {
        "id": tc.get("id") or "",
        "tool_name": tc.get("tool_name") or "unknown",
        "input": scrub_json_text(tc.get("input")),
        "output": scrub_json_text(output),
        "error": reason or None,
        "status": "error" if failed else "completed",
        "timestamp": tc.get("timestamp") or "",
    }
    return shaped, issue if failed else None, reason


def get_transcript(cur, org_id: str, session_id: str) -> Optional[dict]:
    """A chat in the chat UI's message shape, with detected issues; None if not in this org."""
    validate_session_id(session_id)
    cur.execute(
        f"""SELECT {_CHAT_COLUMNS},
                   jsonb_path_query_array({_MESSAGES_ARRAY}, '$[0 to {MAX_TRANSCRIPT_MESSAGES - 1}]') AS messages
            FROM chat_sessions cs LEFT JOIN users u ON u.id = cs.user_id
            WHERE cs.id = %s AND cs.org_id = %s AND cs.is_active = true""",
        (session_id, org_id),
    )
    rows = fetch_dicts(cur)
    if not rows:
        return None
    row = rows[0]

    cur.execute(
        f"""SELECT tool_call_id, status, error_message FROM execution_steps es
            WHERE es.org_id = %s AND {ROOT_SESSION_SQL} = %s AND es.tool_call_id IS NOT NULL""",
        (org_id, session_id),
    )
    steps = {s["tool_call_id"]: s for s in fetch_dicts(cur)}

    messages, issues, seen_calls = [], [], set()
    for index, raw in enumerate(row.get("messages") or []):
        if not isinstance(raw, dict):
            continue
        sender = "user" if raw.get("sender") == "user" else "bot"
        text = raw.get("text") if isinstance(raw.get("text"), str) else ""
        tool_calls = []
        for tc in raw.get("toolCalls") or []:
            if not isinstance(tc, dict):
                continue
            seen_calls.add(tc.get("id"))
            shaped, issue, reason = _transcript_tool_call(tc, steps.get(tc.get("id")))
            tool_calls.append(shaped)
            if issue:
                issues.append({"category": issue, "message_index": index, "tool_call_id": shaped["id"],
                               "tool_name": shaped["tool_name"], "summary": reason})
        reply_issue = classify_reply(text) if sender == "bot" else None
        if reply_issue:
            issues.append({"category": reply_issue, "message_index": index, "tool_call_id": None,
                           "tool_name": None, "summary": _snippet(text, ""), "from_reply": True})
        messages.append({"id": index, "sender": sender, "text": scrub(truncate(text, MESSAGE_TEXT_MAX_CHARS)),
                         "timestamp": raw.get("timestamp"), "toolCalls": tool_calls})

    # Failures of sub-agent calls that never reached this transcript.
    for fail in _failed_tool_calls(cur, org_id, chat_ids=[session_id], limit=200):
        if fail["tool_call_id"] not in seen_calls:
            issues.append({"category": fail["category"], "message_index": None, "tool_call_id": None,
                           "tool_name": fail["tool_name"], "summary": fail["reason"] or fail["input"]})

    return {
        "chat": {**_chat_summary(row), "truncated": (row.get("message_count") or 0) > MAX_TRANSCRIPT_MESSAGES},
        "messages": messages,
        "issues": issues,
        "labels": {"sources": SOURCE_LABELS, "issues": CATEGORY_LABELS, "statuses": STATUS_LABELS},
    }
