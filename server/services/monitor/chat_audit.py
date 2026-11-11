"""Org-wide chat audit for admins: list, search and read every chat read-only.

Admins use this to review how Aurora answers across all users and channels
(web, Slack, Google Chat, Teams, RCA, Actions, schedules) and to find the
permissions Aurora lacks or the commands a gate refused, without waiting for
anyone to report them.

Everything is scoped to one org (callers set RLS and every query also filters
on ``org_id``), bounded (page sizes, text caps), and redacted before it leaves:
transcripts carry raw tool output, which can contain credentials.

Search uses a GIN full-text index over the message texts
(``idx_chat_sessions_message_fts``, created in ``db_utils``); the expression
here must stay byte-identical to the index expression or Postgres won't use it.
Issue badges and counts come from ``execution_steps`` (structured status and
error), classified by ``issue_classifier``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from services.monitor.issue_classifier import (
    CATEGORY_LABELS,
    ISSUE_CATEGORIES,
    OTHER,
    classify_reply,
    classify_tool_error,
    classify_tool_output,
    sql_category_case,
)
from services.monitor.tool_call_search import (
    PERIOD_INTERVALS,
    failure_reason,
    scrub,
    summarize_input,
)
from utils.query_helpers import clamp_int, fetch_dicts, iso_utc
from utils.text.text_utils import truncate

DEFAULT_PERIOD = "7d"
DEFAULT_PER_PAGE = 25
MAX_PER_PAGE = 100
QUERY_MAX_CHARS = 200
SNIPPET_RADIUS = 120
USERS_FACET_LIMIT = 200

# Transcript caps: one view of one chat, but RCA chats can hold megabytes.
MAX_TRANSCRIPT_MESSAGES = 2000
MESSAGE_TEXT_MAX_CHARS = 20_000
TOOL_INPUT_MAX_CHARS = 2_000
TOOL_OUTPUT_MAX_CHARS = 4_000
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_:-]{1,64}$")
USER_ID_MAX_CHARS = 255

# ---------------------------------------------------------------------------
# Source (what started the chat)
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

# trigger_metadata.source values from alert/incident integrations → an RCA.
_ALERT_SOURCES = (
    "grafana", "pagerduty", "datadog", "sentry", "opsgenie", "newrelic", "cloudwatch",
    "splunk", "netdata", "incidentio", "dynatrace", "bigpanda", "kibana", "servicenow",
    "jira", "spinnaker", "incident", "on_incident", "incident_chat", "stored_event",
)

_TRIGGER_SRC = "LOWER(COALESCE(cs.ui_state->'triggerMetadata'->>'source', ''))"


def _in_list(values) -> str:
    # Constant, code-owned identifiers only — never user input.
    return ", ".join(f"'{v}'" for v in values)


SOURCE_SQL = f"""(CASE
    WHEN cs.incident_id IS NOT NULL THEN 'rca'
    WHEN {_TRIGGER_SRC} IN ('slack', 'slack_button') THEN 'slack'
    WHEN {_TRIGGER_SRC} IN ('google_chat', 'google_chat_button') THEN 'google_chat'
    WHEN {_TRIGGER_SRC} = 'teams' THEN 'teams'
    WHEN {_TRIGGER_SRC} = 'action' THEN 'action'
    WHEN {_TRIGGER_SRC} = 'schedule' THEN 'scheduled'
    WHEN {_TRIGGER_SRC} IN ({_in_list(_ALERT_SOURCES)}) THEN 'rca'
    WHEN {_TRIGGER_SRC} <> '' OR cs.ui_state->>'isBackground' = 'true' THEN 'background'
    ELSE 'web'
END)"""

STATUS_LABELS = {"completed": "Completed", "failed": "Failed", "in_progress": "In progress"}

STATUS_SQL = """(CASE
    WHEN cs.status = 'in_progress' THEN 'in_progress'
    WHEN cs.status IN ('failed', 'error') THEN 'failed'
    ELSE 'completed'
END)"""

# Must match idx_chat_sessions_message_fts exactly (see db_utils).
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


class InvalidChatFilter(ValueError):
    """Carries a key of CHAT_FILTER_ERRORS; responses use the constant message."""

    def __init__(self, code: str):
        super().__init__(CHAT_FILTER_ERRORS[code])
        self.code = code


CHAT_FILTER_ERRORS = {
    "period": f"period must be one of: {', '.join(PERIOD_INTERVALS)}",
    "source": f"source must be one of: {', '.join(SOURCE_LABELS)}, all",
    "status": "status must be one of: completed, failed, in_progress, all",
    "issue": f"issue must be one of: {', '.join(ISSUE_CATEGORIES)}, any, all",
    "user": "user is not a valid user id",
    "session": "Invalid chat id",
}


def chat_filter_error_message(exc: InvalidChatFilter) -> str:
    return CHAT_FILTER_ERRORS.get(getattr(exc, "code", ""), "Invalid filter")


@dataclass
class ChatFilters:
    period: str = DEFAULT_PERIOD
    source: Optional[str] = None
    status: Optional[str] = None
    issue: Optional[str] = None      # a category, "any" (any failed tool call), or None
    user_id: Optional[str] = None
    query: Optional[str] = None
    page: int = 1
    per_page: int = DEFAULT_PER_PAGE

    @classmethod
    def parse(cls, raw: dict) -> "ChatFilters":
        def opt(key):
            value = (raw.get(key) or "").strip()
            return None if value.lower() in ("", "all") else value

        period = (opt("period") or DEFAULT_PERIOD).lower()
        if period not in PERIOD_INTERVALS:
            raise InvalidChatFilter("period")
        source = (opt("source") or "").lower() or None
        if source and source not in SOURCE_LABELS:
            raise InvalidChatFilter("source")
        status = (opt("status") or "").lower() or None
        if status and status not in STATUS_LABELS:
            raise InvalidChatFilter("status")
        issue = (opt("issue") or "").lower() or None
        if issue and issue not in ISSUE_CATEGORIES and issue != "any":
            raise InvalidChatFilter("issue")
        user_id = opt("user")
        if user_id and len(user_id) > USER_ID_MAX_CHARS:
            raise InvalidChatFilter("user")
        query = opt("q")
        return cls(
            period=period,
            source=source,
            status=status,
            issue=issue,
            user_id=user_id,
            query=query[:QUERY_MAX_CHARS] if query else None,
            page=clamp_int(raw.get("page"), 1, 1, 10_000),
            per_page=clamp_int(raw.get("per_page"), DEFAULT_PER_PAGE, 1, MAX_PER_PAGE),
        )


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


_LIST_SQL = f"""
    SELECT cs.id, cs.title, cs.user_id, u.email AS user_email, u.name AS user_name,
           {SOURCE_SQL} AS source, {_TRIGGER_SRC} AS trigger_source,
           cs.created_at, cs.updated_at, {STATUS_SQL} AS status, cs.incident_id,
           CASE WHEN jsonb_typeof(cs.messages) = 'array'
                THEN jsonb_array_length(cs.messages) ELSE 0 END AS message_count
    FROM chat_sessions cs
    LEFT JOIN users u ON u.id = cs.user_id
    WHERE {{where}}
    ORDER BY cs.updated_at DESC, cs.id DESC
    LIMIT %s OFFSET %s
"""


def _snippet(text: str, query: str) -> str:
    """A window of ``text`` around the first query term, redacted."""
    terms = [t for t in re.findall(r"[\w.-]+", query.lower()) if len(t) > 1 and t not in ("or", "and")]
    lower = text.lower()
    pos = min((p for p in (lower.find(t) for t in terms) if p >= 0), default=0)
    start = max(0, pos - SNIPPET_RADIUS)
    end = min(len(text), pos + SNIPPET_RADIUS)
    snippet = text[start:end].replace("\n", " ").strip()
    return scrub(("…" if start else "") + snippet + ("…" if end < len(text) else ""))


def _issue_counts_for(cur, org_id: str, session_ids: list[str]) -> dict[str, dict[str, int]]:
    if not session_ids:
        return {}
    case_sql, case_params = sql_category_case("es.error_message")
    cur.execute(
        f"""SELECT {ROOT_SESSION_SQL} AS sid, {case_sql} AS category, COUNT(*)
            FROM execution_steps es
            WHERE es.org_id = %s AND es.status = 'error' AND {ROOT_SESSION_SQL} = ANY(%s)
            GROUP BY 1, 2""",
        tuple(case_params + [org_id, session_ids]),
    )
    out: dict[str, dict[str, int]] = {}
    for sid, category, count in cur.fetchall():
        out.setdefault(sid, {})[category] = count
    return out


def _snippets_for(cur, org_id: str, session_ids: list[str], query: str) -> dict[str, dict]:
    """First matching message per chat (bounded to the page's chats)."""
    if not session_ids or not query:
        return {}
    cur.execute(
        """SELECT cs.id, hit.msg_index, hit.sender, hit.text
           FROM chat_sessions cs
           JOIN LATERAL (
               SELECT (e.idx - 1)::int AS msg_index, e.m->>'sender' AS sender,
                      LEFT(e.m->>'text', %s) AS text
               FROM jsonb_array_elements(
                        CASE WHEN jsonb_typeof(cs.messages) = 'array' THEN cs.messages
                             ELSE '[]'::jsonb END
                    ) WITH ORDINALITY AS e(m, idx)
               WHERE to_tsvector('simple', COALESCE(e.m->>'text', ''))
                     @@ websearch_to_tsquery('simple', %s)
               ORDER BY e.idx
               LIMIT 1
           ) hit ON true
           WHERE cs.org_id = %s AND cs.id = ANY(%s)""",
        (MESSAGE_TEXT_MAX_CHARS, query, org_id, session_ids),
    )
    return {
        sid: {"message_index": idx, "sender": _sender(sender), "text": _snippet(text or "", query)}
        for sid, idx, sender, text in cur.fetchall()
    }


def _sender(raw) -> str:
    return "user" if raw == "user" else "aurora"


def list_chats(cur, org_id: str, f: ChatFilters) -> dict:
    """One page of the org's chats, plus facets and per-category issue counts."""
    where, params = _chat_where(f, org_id)

    cur.execute(f"SELECT COUNT(*) FROM chat_sessions cs WHERE {where}", tuple(params))
    total = cur.fetchone()[0] or 0

    offset = (f.page - 1) * f.per_page
    cur.execute(_LIST_SQL.format(where=where), tuple(params + [f.per_page, offset]))
    rows = fetch_dicts(cur)
    ids = [r["id"] for r in rows]

    issues = _issue_counts_for(cur, org_id, ids)
    snippets = _snippets_for(cur, org_id, ids, f.query or "")

    chats = []
    for r in rows:
        by_cat = issues.get(r["id"], {})
        chats.append({
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
            "issues": {cat: n for cat, n in by_cat.items() if cat != OTHER},
            "failed_tool_calls": sum(by_cat.values()),
            "match": snippets.get(r["id"]),
        })

    return {
        "chats": chats,
        "total": total,
        "page": f.page,
        "per_page": f.per_page,
        "total_pages": max(1, -(-total // f.per_page)),
        "issue_counts": _issue_totals(cur, org_id, f),
        "sources": _source_facet(cur, org_id, f),
        "users": _user_facet(cur, org_id, f),
        "labels": {"sources": SOURCE_LABELS, "issues": CATEGORY_LABELS, "statuses": STATUS_LABELS},
    }


def _issue_totals(cur, org_id: str, f: ChatFilters) -> list[dict]:
    """Failed tool calls per category in the period, within the source/user filters."""
    case_sql, case_params = sql_category_case("es.error_message")
    where, params = _chat_where(f, org_id, skip=("status", "issue", "query"))
    cur.execute(
        f"""SELECT {case_sql} AS category, COUNT(*) AS calls,
                   COUNT(DISTINCT {ROOT_SESSION_SQL}) AS chats
            FROM execution_steps es
            JOIN chat_sessions cs ON cs.id = {ROOT_SESSION_SQL}
            WHERE es.org_id = %s AND es.status = 'error'
              AND es.started_at >= NOW() - %s::interval
              AND {where}
            GROUP BY 1""",
        tuple(case_params + [org_id, PERIOD_INTERVALS[f.period]] + params),
    )
    found = {cat: (calls, chats) for cat, calls, chats in cur.fetchall()}
    return [
        {"category": cat, "label": CATEGORY_LABELS[cat],
         "calls": found.get(cat, (0, 0))[0], "chats": found.get(cat, (0, 0))[1]}
        for cat in (*ISSUE_CATEGORIES, OTHER)
    ]


def _source_facet(cur, org_id: str, f: ChatFilters) -> list[dict]:
    where, params = _chat_where(f, org_id, skip=("source",))
    cur.execute(
        f"SELECT {SOURCE_SQL} AS source, COUNT(*) FROM chat_sessions cs WHERE {where} GROUP BY 1",
        tuple(params),
    )
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

def validate_session_id(session_id: str) -> str:
    if not session_id or not SESSION_ID_RE.match(session_id):
        raise InvalidChatFilter("session")
    return session_id


def _tool_call(tc: dict, step: Optional[dict]) -> dict:
    output = tc.get("output")
    if output is not None and not isinstance(output, str):
        output = str(output)
    step_failed = bool(step and step.get("status") == "error")
    error_text = (step or {}).get("error_message") or output or ""
    if step_failed:
        issue = classify_tool_error(error_text)
    else:
        issue = classify_tool_output(output)
    failed = step_failed or issue is not None
    if step:
        status = {"error": "failed", "success": "succeeded"}.get(step.get("status"), step.get("status"))
    else:
        status = "failed" if failed else (tc.get("status") or "unknown")
    return {
        "id": tc.get("id"),
        "tool_name": tc.get("tool_name") or "unknown",
        "input": scrub(truncate(summarize_input(tc.get("input")), TOOL_INPUT_MAX_CHARS)),
        "output": scrub(truncate(output, TOOL_OUTPUT_MAX_CHARS)) if output else "",
        "status": status,
        "issue": issue if failed else None,
        "reason": scrub(failure_reason(error_text)) if failed else "",
    }


def get_transcript(cur, org_id: str, session_id: str) -> Optional[dict]:
    """A chat's messages with tool calls and detected issues, or None if not in this org."""
    validate_session_id(session_id)
    cur.execute(
        f"""SELECT cs.id, cs.title, cs.user_id, u.email, u.name, {SOURCE_SQL} AS source,
                   {_TRIGGER_SRC} AS trigger_source, {STATUS_SQL} AS status,
                   cs.created_at, cs.updated_at, cs.incident_id,
                   CASE WHEN jsonb_typeof(cs.messages) = 'array'
                        THEN jsonb_array_length(cs.messages) ELSE 0 END AS message_count,
                   CASE WHEN jsonb_typeof(cs.messages) = 'array'
                        THEN jsonb_path_query_array(cs.messages, '$[0 to {MAX_TRANSCRIPT_MESSAGES - 1}]')
                        ELSE '[]'::jsonb END AS messages
            FROM chat_sessions cs LEFT JOIN users u ON u.id = cs.user_id
            WHERE cs.id = %s AND cs.org_id = %s AND cs.is_active = true""",
        (session_id, org_id),
    )
    rows = fetch_dicts(cur)
    if not rows:
        return None
    row = rows[0]

    cur.execute(
        f"""SELECT tool_call_id, tool_name, tool_input, status, error_message, started_at
            FROM execution_steps es
            WHERE es.org_id = %s AND {ROOT_SESSION_SQL} = %s
            ORDER BY es.started_at ASC, es.id ASC""",
        (org_id, session_id),
    )
    steps = fetch_dicts(cur)
    steps_by_call = {s["tool_call_id"]: s for s in steps if s.get("tool_call_id")}

    messages, issues, seen_calls = [], [], set()
    for index, raw in enumerate(row.get("messages") or []):
        if not isinstance(raw, dict):
            continue
        sender = _sender(raw.get("sender"))
        text = raw.get("text") if isinstance(raw.get("text"), str) else ""
        tool_calls = []
        for tc in raw.get("toolCalls") or []:
            if not isinstance(tc, dict):
                continue
            seen_calls.add(tc.get("id"))
            shaped = _tool_call(tc, steps_by_call.get(tc.get("id")))
            tool_calls.append(shaped)
            if shaped["issue"]:
                issues.append({
                    "category": shaped["issue"], "message_index": index,
                    "tool_call_id": shaped["id"], "tool_name": shaped["tool_name"],
                    "summary": shaped["reason"] or shaped["input"],
                })
        reply_issue = classify_reply(text) if sender == "aurora" else None
        if reply_issue:
            issues.append({
                "category": reply_issue, "message_index": index, "tool_call_id": None,
                "tool_name": None, "summary": _snippet(text, "") if text else "",
                "from_reply": True,
            })
        messages.append({
            "index": index,
            "sender": sender,
            "text": scrub(truncate(text, MESSAGE_TEXT_MAX_CHARS)),
            "timestamp": raw.get("timestamp"),
            "tool_calls": tool_calls,
            "issue": reply_issue,
        })

    # Failures recorded for sub-agents or calls that never reached the transcript.
    other_failures = [
        {
            "tool_name": s.get("tool_name") or "unknown",
            "input": scrub(summarize_input(s.get("tool_input"))),
            "category": classify_tool_error(s.get("error_message")),
            "reason": scrub(failure_reason(s.get("error_message"))),
            "started_at": iso_utc(s.get("started_at")),
        }
        for s in steps
        if s.get("status") == "error" and s.get("tool_call_id") not in seen_calls
    ]
    for failure in other_failures:
        if failure["category"] != OTHER:
            issues.append({
                "category": failure["category"], "message_index": None, "tool_call_id": None,
                "tool_name": failure["tool_name"], "summary": failure["reason"] or failure["input"],
            })

    return {
        "chat": {
            "id": row["id"],
            "title": row.get("title") or "Untitled chat",
            "user": {"id": row.get("user_id"), "email": row.get("email"), "name": row.get("name")},
            "source": row["source"],
            "trigger_source": row.get("trigger_source") or None,
            "status": row["status"],
            "created_at": iso_utc(row.get("created_at")),
            "updated_at": iso_utc(row.get("updated_at")),
            "incident_id": str(row["incident_id"]) if row.get("incident_id") else None,
            "message_count": row.get("message_count") or 0,
            "truncated": (row.get("message_count") or 0) > MAX_TRANSCRIPT_MESSAGES,
        },
        "messages": messages,
        "issues": issues,
        "other_failed_tool_calls": other_failures,
        "labels": {"sources": SOURCE_LABELS, "issues": CATEGORY_LABELS, "statuses": STATUS_LABELS},
    }
