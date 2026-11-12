"""Org-wide chat audit: every chat, its failed tool calls, and the issues behind them.

One query layer serves two callers:

* the monitor page's Chat history tab: ``list_chats``, ``list_tool_calls``
  and ``get_transcript``;
* the agent's read-only audit tools (``search_conversations``,
  ``search_tool_calls``, ``get_conversation``), which call the same three
  functions so a question in chat gets the page's answer.

Who may call them is decided by ``utils.auth.viewer_run.may_audit_chats``
(admins, or admins and editors, per the org's setting). This module only
answers; it never widens or narrows access. Both callers share the same
scoping (every query filters on ``org_id``; callers also set RLS), the same
issue classification (``issue_classifier``) and the same redaction
(``scrub``). Nothing leaves this module unredacted: transcripts and tool
errors routinely carry credentials.

Full-text search uses a GIN index over the message texts
(``idx_chat_sessions_message_fts``, built in ``utils.db.chat_audit_indexes``,
which also defines ``SEARCH_DOC_SQL`` so the two can't drift apart).
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
from utils.db.chat_audit_indexes import SEARCH_DOC_SQL  # identical to the GIN index expression
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
SNIPPET_WINDOW = 4000  # chars fetched around a search match
USERS_FACET_LIMIT = 200
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

# Redaction always runs on the *full* text before anything is cut: a window or a
# truncation that drops ``--password`` but keeps its value, or keeps only part of
# a key, would defeat the patterns.
#
# ``redact`` (gitleaks rules) catches token *shapes*; these catch a plaintext
# secret that only its flag or key name gives away (``--password hunter2``).
_SECRET_NAMES = (
    r"password|passwd|pwd|token|secret|api[-_]?key|access[-_]?key|"
    r"secret[-_]?key|client[-_]?secret|authorization|auth"
)
_SECRET_FLAG_RE = re.compile(rf"(?i)(--?(?:{_SECRET_NAMES})(?:[= ]+))(\"[^\"]*\"|'[^']*'|\S+)")
_SECRET_KV_RE = re.compile(rf"(?i)(\"?(?:{_SECRET_NAMES})\"?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,}}]+)")
# JSON key names whose values are credentials, matched as parts of a name:
# SecretAccessKey, SessionToken, DB_PASS, tls.key, privateKey, client_secret.
_SECRET_KEY_RE = re.compile(
    r"(?i)secret|token|passw|pwd|credential|auth(?!or(?!iz))|(^|[^a-z])pass$|key(s)?$|key[-_.]"
)
# Kubernetes Secret objects: every value under these keys is secret material.
_K8S_SECRET_DATA_KEYS = ("data", "stringData")
REDACTED = "[REDACTED]"


def scrub(text: Optional[str]) -> str:
    """Remove secrets from free text (a command, an error, a message)."""
    if not text:
        return ""
    cleaned, _ = redact(text)
    cleaned = _SECRET_FLAG_RE.sub(lambda m: f"{m.group(1)}{REDACTED}", cleaned)
    return _SECRET_KV_RE.sub(lambda m: f"{m.group(1)}{REDACTED}", cleaned)


def _scrub_value(value):
    """Redact a parsed JSON value: secret-named keys entirely, every string by pattern."""
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, list):
        return [_scrub_value(v) for v in value]
    if not isinstance(value, dict):
        return value
    is_k8s_secret = value.get("kind") == "Secret"
    out = {}
    for k, v in value.items():
        secret_key = isinstance(k, str) and (
            _SECRET_KEY_RE.search(k) or (is_k8s_secret and k in _K8S_SECRET_DATA_KEYS)
        )
        out[k] = REDACTED if secret_key and v not in (None, "", [], {}) else _scrub_value(v)
    return out


def scrub_json_text(text, limit: int = TOOL_IO_MAX_CHARS) -> str:
    """Scrub a tool input/output, keeping it valid JSON when it is JSON, then cap it.

    The chat's tool widgets parse these strings per tool, so redaction happens
    on the parsed values; the serialized text is scrubbed again as a backstop.
    """
    if not text:
        return ""
    if not isinstance(text, str):
        text = json.dumps(text, default=str)
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, (dict, list)):
        return truncate(json.dumps(_scrub_value(parsed), default=str), limit)
    return truncate(scrub(text), limit)


def summarize_input(tool_input) -> str:
    """The command a tool ran, or a compact rendering of its arguments; redacted, then capped."""
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except (ValueError, TypeError):
            return truncate(scrub(tool_input), INPUT_SUMMARY_MAX_CHARS)
    tool_input = _scrub_value(tool_input)
    command = derive_command(tool_input, INPUT_SUMMARY_MAX_CHARS)
    if command:
        return command
    if isinstance(tool_input, dict) and tool_input:
        return truncate(json.dumps(tool_input, default=str, sort_keys=True), INPUT_SUMMARY_MAX_CHARS)
    return "" if tool_input in (None, {}, "") else truncate(str(tool_input), INPUT_SUMMARY_MAX_CHARS)


# Where tools put the useful part of an error, most specific first.
# terminal_exec: {"success": false, "chat_output": "<stderr>", ...}
# cloud_exec multi-target: {"results_by_subscription": {"<id>": {"success": false, "error"|"output": ...}}}
_ERROR_TEXT_KEYS = ("error", "message", "detail", "details", "stderr", "chat_output", "output", "stdout")
_MULTI_RESULT_KEYS = ("results_by_subscription", "results_by_account")


def _error_text(value, depth: int = 0) -> str:
    """The human-readable error inside a tool's JSON error payload, or ''."""
    if depth > 4:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return next((t for t in (_error_text(v, depth + 1) for v in value) if t), "")
    if not isinstance(value, dict):
        return ""
    for key in _MULTI_RESULT_KEYS:
        results = value.get(key)
        if isinstance(results, dict):
            for target, result in results.items():
                if isinstance(result, dict) and result.get("success") is False:
                    text = _error_text(result, depth + 1)
                    if text:
                        return f"{target}: {text}"
    for key in (*_ERROR_TEXT_KEYS, "errors"):
        text = _error_text(value.get(key), depth + 1)
        if text:
            return text
    return ""


def failure_reason(error: Optional[str]) -> str:
    """One redacted, readable line from a stored error body.

    Tool errors are often JSON (``{"error": ...}``, terminal_exec's
    ``chat_output``, per-subscription results); the useful text is pulled out
    of the envelope, so the reason is never just ``{``.
    """
    if not error:
        return ""
    text = error.strip()
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, (dict, list)):
        # Nothing readable inside: show the compact, redacted payload instead.
        text = _error_text(parsed) or json.dumps(_scrub_value(parsed), default=str)
    text = scrub(text)
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return truncate(first, REASON_MAX_CHARS)


def _terms(query: str) -> list[str]:
    return [t for t in re.findall(r"[\w.-]+", query.lower()) if len(t) > 1 and t not in ("or", "and")]


def _snippet(text: str, query: str) -> str:
    """A window of ``text`` around the first query term (or its start), cut after redaction."""
    text = scrub(text)
    terms = _terms(query)
    lower = text.lower()
    pos = min((p for p in (lower.find(t) for t in terms) if p >= 0), default=0)
    start, end = max(0, pos - SNIPPET_RADIUS), min(len(text), pos + SNIPPET_RADIUS)
    snippet = text[start:end].replace("\n", " ").strip()
    return ("…" if start else "") + snippet + ("…" if end < len(text) else "")


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
# Tool-call (execution_steps) statuses: UI name → stored value.
STEP_STATUSES = {"failed": "error", "succeeded": "success", "running": "running"}
STEP_STATUS_LABELS = {"succeeded": "Succeeded", "failed": "Failed", "running": "Running"}

# Constant, user-facing messages: responses look them up by code and never
# echo exception text.
FILTER_ERRORS = {
    "step_status": "status must be one of: failed, succeeded, running, all",
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
        # Same window as the counts and badges: a failure from before the
        # period doesn't match just because the chat had new activity since.
        exists = (
            "EXISTS (SELECT 1 FROM execution_steps es "
            f"WHERE es.org_id = cs.org_id AND {ROOT_SESSION_SQL} = cs.id AND es.status = 'error' "
            "AND es.started_at >= NOW() - %s::interval"
        )
        params.append(PERIOD_INTERVALS[f.period])
        if f.issue == "any":
            clauses.append(exists + ")")
        else:
            case_sql, case_params = sql_category_case("es.error_message")
            clauses.append(exists + f" AND {case_sql} = %s)")
            params.extend(case_params + [f.issue])
    return " AND ".join(clauses), params


# ---------------------------------------------------------------------------
# Tool calls (shared by the page and the agent tools)
# ---------------------------------------------------------------------------

# A failure from a chat the user deleted (soft delete) stays out of every view.
# RCA rows are kept: their incident is the anchor, not the chat.
_NOT_DELETED_SQL = (
    "(es.incident_id IS NOT NULL OR EXISTS (SELECT 1 FROM chat_sessions act "
    "WHERE act.id = split_part(es.session_id, '::', 1) AND act.org_id = es.org_id AND act.is_active))"
)
# Most recent rows examined when filtering by free text (matched in Python on
# redacted fields, never in SQL on raw stored text).
QUERY_POOL_SIZE = 500


def _matches_query(failure: dict, query: str) -> bool:
    """Free text matched against what the caller can see: never raw inputs or errors."""
    haystack = " ".join(
        (failure["tool_name"], failure["category"] or "", failure["input"], failure["reason"])
    ).lower()
    return query.lower() in haystack


# Transcript message holding each tool call of the chats in CTE ``f``: one pass
# over each chat's messages. Takes the org as its one parameter.
_ANCHORS_SQL = """
            SELECT c.id AS chat_id, tc->>'id' AS tool_call_id, MIN(e.idx - 1)::int AS message_index
            FROM chat_sessions c
            CROSS JOIN LATERAL jsonb_array_elements(
                CASE WHEN jsonb_typeof(c.messages) = 'array' THEN c.messages ELSE '[]'::jsonb END
            ) WITH ORDINALITY AS e(m, idx)
            CROSS JOIN LATERAL jsonb_array_elements(
                CASE WHEN jsonb_typeof(e.m->'toolCalls') = 'array' THEN e.m->'toolCalls' ELSE '[]'::jsonb END
            ) AS tc
            WHERE c.org_id = %s AND c.id IN (SELECT DISTINCT chat_id FROM f)
            GROUP BY 1, 2"""


def link_for(chat_id: Optional[str], incident_id: Optional[str]) -> Optional[str]:
    """App-relative link to where a chat or tool call can be opened."""
    if incident_id:
        return f"/incidents/{incident_id}"
    return f"/chat?sessionId={chat_id}" if chat_id else None


# Channels whose answers stay with the person who asked. Anywhere else (a Slack
# channel, Teams, Google Chat, a background run that may post to one), the agent
# tools return metadata and links only, never other people's message text.
def private_answer_channel(trigger_metadata: Optional[dict]) -> bool:
    if trigger_metadata is None:
        return True                                  # the web app sets none
    if not isinstance(trigger_metadata, dict):
        return False                                 # malformed: fail closed
    meta = trigger_metadata
    source = str(meta.get("source") or "").lower()
    if source in ("", "chat"):
        return True                                  # the web app
    if source == "slack":
        # Slack direct-message channel ids start with "D"; anything else is shared.
        return str(meta.get("channel") or "").startswith("D")
    return False


def _shape_failure(r: dict) -> dict:
    """A failed execution_steps row as returned to callers: classified and redacted."""
    return {
        "chat_id": r["chat_id"],
        "incident_id": str(r["incident_id"]) if r.get("incident_id") else None,
        "message_index": r.get("message_index"),
        "tool_call_id": r.get("tool_call_id"),
        "tool_name": r.get("tool_name") or "unknown",
        "input": summarize_input(r.get("tool_input")),
        "category": classify_tool_error(r.get("error_message")),
        "reason": failure_reason(r.get("error_message")),
        "started_at": iso_utc(r.get("started_at")),
    }


def _shape_step(r: dict) -> dict:
    """Any execution_steps row: failures get their issue and reason, others don't."""
    status = r.get("status")
    if status == "error":
        return {**_shape_failure(r), "status": "failed"}
    return {
        **_shape_failure({**r, "error_message": None}),
        "category": None,
        "reason": "",
        "status": {"success": "succeeded", "running": "running"}.get(status, status),
    }


def _failed_tool_calls(cur, org_id: str, chat_ids: list[str], limit: int) -> list[dict]:
    """A chat's failed ``execution_steps`` rows (sub-agents included), newest first.

    Classified, redacted and anchored: ``message_index`` is the transcript
    message holding the call, or None for sub-agent calls that never reached it.
    """
    cur.execute(
        f"""WITH f AS (
                SELECT es.id, es.tool_name, es.tool_input, es.error_message, es.started_at,
                       es.tool_call_id, es.incident_id, {ROOT_SESSION_SQL} AS chat_id
                FROM execution_steps es
                WHERE es.org_id = %s AND es.status = 'error' AND {_NOT_DELETED_SQL}
                  AND {ROOT_SESSION_SQL} = ANY(%s)
                ORDER BY es.started_at DESC, es.id DESC
                LIMIT %s
            ), anchors AS ({_ANCHORS_SQL}
            )
            SELECT f.*, a.message_index
            FROM f LEFT JOIN anchors a ON a.chat_id = f.chat_id AND a.tool_call_id = f.tool_call_id
            ORDER BY f.started_at DESC, f.id DESC""",
        (org_id, chat_ids, limit, org_id),
    )
    return [_shape_failure(r) for r in fetch_dicts(cur)]


def _issue_totals(cur, org_id: str, period: str, chat_where: str = "TRUE", chat_params=()) -> list[dict]:
    """Failed tool calls per issue category in ``period`` (optionally within chat filters)."""
    case_sql, case_params = sql_category_case("es.error_message")
    cur.execute(
        f"""SELECT {case_sql} AS category, COUNT(*), COUNT(DISTINCT {ROOT_SESSION_SQL})
            FROM execution_steps es
            LEFT JOIN chat_sessions cs ON cs.id = {ROOT_SESSION_SQL} AND cs.org_id = es.org_id
            WHERE es.org_id = %s AND es.status = 'error' AND {_NOT_DELETED_SQL}
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


# ---------------------------------------------------------------------------
# Tool calls list (monitor page and agent tools)
# ---------------------------------------------------------------------------

def list_tool_calls(cur, org_id: str, raw: dict) -> dict:
    """One page of recorded tool calls (any status), newest first, with chat, user and source.

    Filters: period, status (failed | succeeded | running), issue, source, user,
    tool and free text. An issue (a category, or "any") implies failed. Free text
    matches the redacted tool, command and reason (never raw stored text), so
    with ``q`` the page is taken from the ``QUERY_POOL_SIZE`` most recent
    matches of the other filters, and ``search_capped`` says when that pool was full.

    ``total`` and ``status_counts`` cover every status; ``issue_counts`` covers
    failures only.
    """
    # ``status`` here is the tool call's status, not a chat's, so it's checked
    # separately from the shared chat filters.
    step_status = (_opt(raw, "status") or "").lower() or None
    if step_status and step_status not in STEP_STATUSES:
        raise InvalidFilter("step_status")
    f = ChatFilters.parse({k: v for k, v in raw.items() if k != "status"})
    if f.issue:
        step_status = "failed"
    tool = _opt(raw, "tool")
    case_sql, case_params = sql_category_case("es.error_message")

    # Filters on the chat a call belongs to (source, user) and on the call (tool).
    scope, scope_params = ["TRUE"], []
    if f.source:
        scope.append(f"{SOURCE_SQL} = %s")
        scope_params.append(f.source)
    if f.user_id:
        scope.append("cs.user_id = %s")
        scope_params.append(f.user_id)
    if tool:
        scope.append("es.tool_name = %s")
        scope_params.append(tool)
    base = ["es.org_id = %s", _NOT_DELETED_SQL, "es.started_at >= NOW() - %s::interval", *scope]
    base_params = [org_id, PERIOD_INTERVALS[f.period], *scope_params]
    where, params = list(base), list(base_params)
    if step_status:
        where.append("es.status = %s")
        params.append(STEP_STATUSES[step_status])
    if f.issue and f.issue != "any":
        where.append(f"{case_sql} = %s")
        params.extend(case_params + [f.issue])
    from_sql = f"""FROM execution_steps es
            LEFT JOIN chat_sessions cs ON cs.id = {ROOT_SESSION_SQL} AND cs.org_id = es.org_id
            WHERE {" AND ".join(where)}"""

    if f.query:
        limit, offset = QUERY_POOL_SIZE, 0
    else:
        cur.execute(f"SELECT COUNT(*) {from_sql}", tuple(params))
        total = cur.fetchone()[0] or 0
        limit, offset = f.per_page, (f.page - 1) * f.per_page
    cur.execute(
        f"""WITH f AS (
                SELECT es.id, es.tool_name, es.tool_input, es.status, es.error_message, es.started_at,
                       es.tool_call_id, es.incident_id, {ROOT_SESSION_SQL} AS chat_id,
                       cs.title AS chat_title, cs.user_id, {SOURCE_SQL} AS source
                {from_sql}
                ORDER BY es.started_at DESC, es.id DESC
                LIMIT %s OFFSET %s
            ), anchors AS ({_ANCHORS_SQL}
            )
            SELECT f.*, a.message_index, u.email AS user_email, u.name AS user_name
            FROM f
            LEFT JOIN anchors a ON a.chat_id = f.chat_id AND a.tool_call_id = f.tool_call_id
            LEFT JOIN users u ON u.id = f.user_id
            ORDER BY f.started_at DESC, f.id DESC""",
        tuple(params + [limit, offset, org_id]),
    )
    rows = [
        {
            **_shape_step(r),
            "link": link_for(r.get("chat_id"), str(r["incident_id"]) if r.get("incident_id") else None),
            "chat_title": scrub(r.get("chat_title")) or "Untitled chat",
            "source": r.get("source"),
            "user": {"id": r.get("user_id"), "email": r.get("user_email"), "name": r.get("user_name")},
        }
        for r in fetch_dicts(cur)
    ]
    search_capped = False
    if f.query:
        search_capped = len(rows) >= QUERY_POOL_SIZE
        rows = [r for r in rows if _matches_query(r, f.query)]
        total = len(rows)
        rows = rows[(f.page - 1) * f.per_page: f.page * f.per_page]

    # Per-status counts within the period and scope filters (not status, issue or text).
    cur.execute(
        f"""SELECT es.status, COUNT(*) FROM execution_steps es
            LEFT JOIN chat_sessions cs ON cs.id = {ROOT_SESSION_SQL} AND cs.org_id = es.org_id
            WHERE {" AND ".join(base)} GROUP BY 1""",
        tuple(base_params),
    )
    by_status = dict(cur.fetchall())
    status_counts = {name: by_status.get(stored, 0) for name, stored in STEP_STATUSES.items()}
    status_counts["total"] = sum(by_status.values())

    counts_where = " AND ".join(scope)
    return {
        "tool_calls": rows,
        "total": total,
        "page": f.page,
        "per_page": f.per_page,
        "total_pages": max(1, -(-total // f.per_page)),
        "search_capped": search_capped,
        "status_counts": status_counts,
        "issue_counts": _issue_totals(cur, org_id, f.period, counts_where, scope_params),
        "tools": _step_facet(cur, org_id, f.period, "es.tool_name", scope, scope_params, skip="es.tool_name"),
        "sources": _step_facet(cur, org_id, f.period, SOURCE_SQL, scope, scope_params, skip=SOURCE_SQL),
        "users": _step_facet(cur, org_id, f.period, "cs.user_id", scope, scope_params, skip="cs.user_id",
                             with_user=True),
        "labels": {"sources": SOURCE_LABELS, "issues": CATEGORY_LABELS, "statuses": STEP_STATUS_LABELS},
    }


def _step_facet(cur, org_id: str, period: str, expr: str, scope: list[str], scope_params: list,
                *, skip: str, with_user: bool = False) -> list[dict]:
    """Tool-call counts by ``expr`` in the period, within every scope filter but its own."""
    kept = [(c, p) for c, p in zip(scope[1:], scope_params) if not c.startswith(skip)]
    extra = "".join(f" AND {c}" for c, _ in kept)
    user_cols = ", MAX(u.email), MAX(u.name)" if with_user else ""
    user_join = "LEFT JOIN users u ON u.id = cs.user_id" if with_user else ""
    cur.execute(
        f"""SELECT {expr} AS value, COUNT(*){user_cols}
            FROM execution_steps es
            LEFT JOIN chat_sessions cs ON cs.id = {ROOT_SESSION_SQL} AND cs.org_id = es.org_id
            {user_join}
            WHERE es.org_id = %s AND {_NOT_DELETED_SQL}
              AND es.started_at >= NOW() - %s::interval{extra}
            GROUP BY 1 ORDER BY 2 DESC LIMIT %s""",
        tuple([org_id, PERIOD_INTERVALS[period], *[p for _, p in kept], USERS_FACET_LIMIT]),
    )
    out = []
    for row in cur.fetchall():
        if row[0] is None:
            continue
        item = {"value": row[0], "count": row[1]}
        if with_user:
            item.update(email=row[2], name=row[3])
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# Conversations list
# ---------------------------------------------------------------------------

def _issue_counts_for(cur, org_id: str, chat_ids: list[str], period: str) -> dict[str, dict[str, int]]:
    case_sql, case_params = sql_category_case("es.error_message")
    cur.execute(
        f"""SELECT {ROOT_SESSION_SQL}, {case_sql}, COUNT(*)
            FROM execution_steps es
            WHERE es.org_id = %s AND es.status = 'error' AND {ROOT_SESSION_SQL} = ANY(%s)
              AND es.started_at >= NOW() - %s::interval
            GROUP BY 1, 2""",
        tuple(case_params + [org_id, chat_ids, PERIOD_INTERVALS[period]]),
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
                -- A bounded window around the first term, wherever it is in the message.
                SELECT (e.idx - 1)::int AS msg_index, e.m->>'sender' AS sender,
                       substr(e.m->>'text',
                              greatest(strpos(lower(e.m->>'text'), %s) - %s, 1), %s) AS text
                FROM jsonb_array_elements({_MESSAGES_ARRAY}) WITH ORDINALITY AS e(m, idx)
                WHERE to_tsvector('simple', COALESCE(e.m->>'text', ''))
                      @@ websearch_to_tsquery('simple', %s)
                ORDER BY e.idx LIMIT 1
            ) hit ON true
            WHERE cs.org_id = %s AND cs.id = ANY(%s)""",
        ((_terms(query) or [""])[0], SNIPPET_WINDOW // 2, SNIPPET_WINDOW, query, org_id, chat_ids),
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
        "title": scrub(r.get("title")) or "Untitled chat",
        "user": {"id": r.get("user_id"), "email": r.get("user_email"), "name": r.get("user_name")},
        "source": r["source"],
        "trigger_source": r.get("trigger_source") or None,
        "status": r["status"],
        "created_at": iso_utc(r.get("created_at")),
        "updated_at": iso_utc(r.get("updated_at")),
        "message_count": r.get("message_count") or 0,
        "incident_id": str(r["incident_id"]) if r.get("incident_id") else None,
        "link": link_for(r["id"], str(r["incident_id"]) if r.get("incident_id") else None),
    }


def list_chats(cur, org_id: str, f: ChatFilters) -> dict:
    """One page of the org's chats with issue badges, plus facets and counts."""
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

    issues, snippets = {}, {}
    if ids:
        issues = _issue_counts_for(cur, org_id, ids, f.period)
        if f.query:
            snippets = _snippets_for(cur, org_id, ids, f.query)

    chats = []
    for r in rows:
        by_cat = issues.get(r["id"], {})
        chats.append({
            **_chat_summary(r),
            "issues": {cat: n for cat, n in by_cat.items() if cat != OTHER},
            "failed_tool_calls": sum(by_cat.values()),
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
    reason = failure_reason(error_text) if failed else ""
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
        messages.append({"id": index, "sender": sender, "text": truncate(scrub(text), MESSAGE_TEXT_MAX_CHARS),
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
