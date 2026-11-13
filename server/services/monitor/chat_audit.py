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
(``idx_chat_sessions_message_text_fts``, built in ``utils.db.chat_audit_indexes``,
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
SEARCH_CANDIDATES = 1000  # most recent word matches ranked by a free-text search
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
# SecretAccessKey, AccessKeyId, SessionToken, DB_PASS, tls.key, privateKey, client_secret.
_SECRET_KEY_RE = re.compile(
    r"(?i)secret|token|passw|pwd|credential|auth(?!or(?!iz))|access[-_]?key|(^|[^a-z])pass$|key(s)?$|key[-_.]"
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
    """The words a match must contain: ``-word`` (excluded by the search) and or/and left out."""
    return [t for t in re.findall(r"[\w.-]+", query.lower())
            if len(t) > 1 and t not in ("or", "and") and not t.startswith("-")]


def _term_groups(query: str) -> list[list[str]]:
    """The query's alternatives as ``websearch_to_tsquery`` reads it: one word list per ``or``."""
    groups = [_terms(alt) for alt in re.split(r"(?i)\s+or\s+", query)]
    return [g for g in groups if g]


# A search reads as a phrase when it is quoted or has this many words; phrase
# hits then rank first and are labelled "exact".
PHRASE_MIN_WORDS = 3


def phrase_of(query: Optional[str]) -> Optional[str]:
    """The words of ``query`` as a phrase to look for, or None for a short unquoted search."""
    q = (query or "").strip()
    quoted = len(q) >= 2 and q[0] == q[-1] == '"'
    words = re.findall(r"\w+", q[1:-1] if quoted else q)
    if words and (quoted or len(words) >= PHRASE_MIN_WORDS):
        return " ".join(words)
    return None


def phrase_pattern(phrase: str) -> re.Pattern:
    """Case-insensitive pattern for a phrase, tolerant of punctuation and line breaks between words."""
    return re.compile(r"\W+".join(re.escape(w) for w in phrase.split()), re.IGNORECASE)


def _best_center(text: str, query: str) -> int:
    """Where to centre a snippet: the phrase if present, else the densest cluster of query words."""
    phrase = phrase_of(query)
    if phrase:
        m = phrase_pattern(phrase).search(text)
        if m:
            return (m.start() + m.end()) // 2
    terms = _terms(query)
    lower = text.lower()
    hits = sorted((m.start(), t) for t in terms for m in re.finditer(re.escape(t), lower))
    if not hits:
        return 0
    best, best_count, j = hits[0][0], 0, 0
    for i, (pos, _) in enumerate(hits):
        while hits[j][0] < pos - 2 * SNIPPET_RADIUS:
            j += 1
        count = len({t for _, t in hits[j:i + 1]})
        if count > best_count:
            best, best_count = (hits[j][0] + pos) // 2, count
    return best


def _snippet(text: str, query: str) -> str:
    """A window of ``text`` around the phrase, or the densest query words, cut after redaction."""
    text = scrub(text)
    center = _best_center(text, query) if query else 0
    start = max(0, center - SNIPPET_RADIUS)
    end = min(len(text), start + 2 * SNIPPET_RADIUS)
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
    "user": "user is not a valid user id or email",
    "session": "Invalid chat id",
    "secret_query": "Search can't look for secret values.",
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
    if not query:
        return None
    query = query[:QUERY_MAX_CHARS]
    # Search runs over raw text, so a query that looks like a secret would let
    # a caller confirm a guessed credential exists. Refuse it outright.
    if scrub(query) != query:
        raise InvalidFilter("secret_query")
    return query


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

# The user filter takes a user id (from a facet) or an email (typed), so any
# member can be picked even when the facet list is capped.
USER_MATCH_SQL = (
    "(cs.user_id = %s OR cs.user_id IN (SELECT um.id FROM users um "
    "WHERE um.org_id = cs.org_id AND lower(um.email) = lower(%s)))"
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
        clauses.append(USER_MATCH_SQL)
        params.extend([f.user_id, f.user_id])
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


def _set_message_anchors(cur, org_id: str, rows: list[dict]) -> None:
    """Fill each tool call's ``message_index``: the transcript message holding it, or None."""
    chat_ids = sorted({r["chat_id"] for r in rows if r.get("chat_id")})
    if not chat_ids:
        return
    cur.execute(
        f"""WITH f AS (SELECT unnest(%s::text[]) AS chat_id), anchors AS ({_ANCHORS_SQL}
            )
            SELECT chat_id, tool_call_id, message_index FROM anchors""",
        (chat_ids, org_id),
    )
    found = {(chat, call): idx for chat, call, idx in cur.fetchall()}
    for r in rows:
        r["message_index"] = found.get((r.get("chat_id"), r.get("tool_call_id")))


def link_for(chat_id: Optional[str], incident_id: Optional[str]) -> Optional[str]:
    """App-relative link to where a chat or tool call can be opened."""
    if incident_id:
        return f"/incidents/{incident_id}"
    return f"/chat?sessionId={chat_id}" if chat_id else None


# Whether an answer stays with the person who asked. Only then may the agent
# tools quote other users' chats; anywhere else (a Slack channel, Teams, Google
# Chat, any background run, including an RCA started from chat that lands on an
# org-visible incident, or a follow-up in an incident's chat, whose tool output
# shows in the incident timeline) they return metadata only.
def private_answer_channel(trigger_metadata: Optional[dict], *, is_background: bool,
                           incident_id: Optional[str] = None) -> bool:
    if incident_id:
        return False                                 # the incident's timeline is org-visible
    if trigger_metadata is not None and not isinstance(trigger_metadata, dict):
        return False                                 # malformed: fail closed
    meta = trigger_metadata or {}
    if str(meta.get("source") or "").lower() == "slack":
        # Slack direct-message channel ids start with "D"; anything else is shared.
        return str(meta.get("channel") or "").startswith("D")
    # The web app: an interactive chat with no trigger at all.
    return not meta and not is_background


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

    # Filters on the chat a call belongs to (source, user) and on the call (tool),
    # by name so each facet can drop exactly its own.
    named: dict[str, tuple[str, list]] = {}
    if f.source:
        named["source"] = (f"{SOURCE_SQL} = %s", [f.source])
    if f.user_id:
        named["user"] = (USER_MATCH_SQL, [f.user_id, f.user_id])
    if tool:
        named["tool"] = ("es.tool_name = %s", [tool])
    scope = ["TRUE", *(sql for sql, _ in named.values())]
    scope_params = [p for _, ps in named.values() for p in ps]
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
            LEFT JOIN chat_sessions cs ON cs.id = {ROOT_SESSION_SQL} AND cs.org_id = es.org_id"""
    where_sql = f"WHERE {' AND '.join(where)}"

    if f.query:
        limit, offset = QUERY_POOL_SIZE, 0
    else:
        cur.execute(f"SELECT COUNT(*) {from_sql} {where_sql}", tuple(params))
        total = cur.fetchone()[0] or 0
        limit, offset = f.per_page, (f.page - 1) * f.per_page
    cur.execute(
        f"""SELECT es.id, es.tool_name, es.tool_input, es.status, es.error_message, es.started_at,
                   es.tool_call_id, es.incident_id, {ROOT_SESSION_SQL} AS chat_id,
                   cs.title AS chat_title, cs.user_id, {SOURCE_SQL} AS source,
                   u.email AS user_email, u.name AS user_name
            {from_sql}
            LEFT JOIN users u ON u.id = cs.user_id
            {where_sql}
            ORDER BY es.started_at DESC, es.id DESC
            LIMIT %s OFFSET %s""",
        tuple(params + [limit, offset]),
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
    # Anchors only for the returned page: with ``q`` the pool is far larger.
    _set_message_anchors(cur, org_id, rows)

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
        "tools": _step_facet(cur, org_id, f.period, "es.tool_name", named, skip="tool"),
        "sources": _step_facet(cur, org_id, f.period, SOURCE_SQL, named, skip="source"),
        "users": _step_facet(cur, org_id, f.period, "cs.user_id", named, skip="user",
                             with_user=True),
        "labels": {"sources": SOURCE_LABELS, "issues": CATEGORY_LABELS, "statuses": STEP_STATUS_LABELS},
    }


def _step_facet(cur, org_id: str, period: str, expr: str, named: dict[str, tuple[str, list]],
                *, skip: str, with_user: bool = False) -> list[dict]:
    """Tool-call counts by ``expr`` in the period, within every named filter but ``skip``."""
    kept = [(sql, ps) for name, (sql, ps) in named.items() if name != skip]
    extra = "".join(f" AND {sql}" for sql, _ in kept)
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
        tuple([org_id, PERIOD_INTERVALS[period], *[p for _, ps in kept for p in ps], USERS_FACET_LIMIT]),
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


# Messages per chat fetched to build a snippet and to check the match survives
# redaction (split across the query's words, at least a few per word), and the
# per-message cap (redaction runs on the whole capped text).
SNIPPET_MESSAGES_PER_CHAT = 50
SNIPPET_MESSAGES_MIN_PER_WORD = 5
SNIPPET_MESSAGE_CHARS = 100_000


def _redacted_match(texts: list[tuple[int, str, str]], query: str) -> Optional[dict]:
    """Best match among one chat's (index, sender, text) messages, judged on redacted text.

    A match needs every word of one ``or`` alternative. Returns None when no
    alternative matches once secrets are redacted, so a search can't confirm
    that a hidden value exists.
    """
    phrase = phrase_of(query)
    redacted = [(idx, sender, scrub(text or "")) for idx, sender, text in texts]
    if phrase:
        pattern = phrase_pattern(phrase)
        for idx, sender, text in redacted:
            if pattern.search(text):
                return {"index": idx, "sender": sender, "text": text, "match_type": "exact"}
    lowered = [(idx, sender, text, text.lower()) for idx, sender, text in redacted]
    groups = _term_groups(query)
    matching = [g for g in groups if all(any(t in low for *_, low in lowered) for t in g)]
    if groups and not matching:
        return None                     # the words only appear inside redacted secrets
    for idx, sender, text, low in lowered:
        if any(all(t in low for t in g) for g in matching):
            return {"index": idx, "sender": sender, "text": text, "match_type": "all_words"}
    # The words are spread across messages: no single message to quote.
    return {"index": None, "sender": None, "text": None, "match_type": "all_words"}


def _snippet_tsqueries(query: str) -> list[str]:
    """``to_tsquery`` strings for the messages worth fetching: each word, and each alternative's words together."""
    queries: list[str] = []
    for group in _term_groups(query):
        words = sorted({w for t in group for w in re.findall(r"\w+", t)})   # \w only: safe in to_tsquery
        for q in (*words, " & ".join(words)):
            if q and q not in queries:
                queries.append(q)
    return queries


def _snippets_for(cur, org_id: str, chat_ids: list[str], query: str) -> dict[str, Optional[dict]]:
    """Per chat: the redacted match ({message_index, sender, text, match_type}), or None.

    Fetches, per chat, the first few messages containing each query word and
    each alternative's words together (so a word first used late in a long
    chat is still seen), redacts each in full, then picks the message (phrase
    first) and cuts the snippet. A chat whose match disappears once redacted
    maps to None so the caller can drop it.
    """
    tsqueries = _snippet_tsqueries(query)
    if not tsqueries:
        return {}
    per_query = max(SNIPPET_MESSAGES_MIN_PER_WORD, SNIPPET_MESSAGES_PER_CHAT // len(tsqueries))
    cur.execute(
        f"""SELECT cs.id, hit.msg_index, hit.sender, hit.text
            FROM chat_sessions cs
            JOIN LATERAL (
                SELECT DISTINCT ON (x.idx) (x.idx - 1)::int AS msg_index, x.m->>'sender' AS sender,
                       LEFT(x.m->>'text', %s) AS text
                FROM (
                    SELECT e.idx, e.m, row_number() OVER (PARTITION BY q.q ORDER BY e.idx) AS nth
                    FROM jsonb_array_elements({_MESSAGES_ARRAY}) WITH ORDINALITY AS e(m, idx)
                    CROSS JOIN LATERAL (SELECT to_tsvector('simple', COALESCE(e.m->>'text', '')) AS doc) d
                    JOIN unnest(%s::text[]) AS q(q) ON d.doc @@ to_tsquery('simple', q.q)
                ) x
                WHERE x.nth <= %s
                ORDER BY x.idx
            ) hit ON true
            WHERE cs.org_id = %s AND cs.id = ANY(%s)
            ORDER BY cs.id, hit.msg_index""",
        (SNIPPET_MESSAGE_CHARS, tsqueries, per_query, org_id, chat_ids),
    )
    by_chat: dict[str, list] = {}
    for sid, idx, sender, text in cur.fetchall():
        by_chat.setdefault(sid, []).append((idx, sender, text))
    out: dict[str, Optional[dict]] = {}
    for sid in chat_ids:
        found = _redacted_match(by_chat.get(sid, []), query)
        if found is None:
            out[sid] = None
        elif found["index"] is None:
            out[sid] = {"message_index": None, "sender": None, "text": None, "match_type": "all_words"}
        else:
            out[sid] = {
                "message_index": found["index"],
                "sender": "user" if found["sender"] == "user" else "aurora",
                # Already redacted in full: _snippet's scrub is a no-op, then it cuts.
                "text": _snippet(found["text"], query),
                "match_type": found["match_type"],
            }
    return out


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
    offset = (f.page - 1) * f.per_page
    search_capped = False
    if f.query:
        # Free text: take the most recent SEARCH_CANDIDATES word matches (GIN
        # index + org/updated_at index), and rank only those, phrase hits
        # first. Ranking recomputes the full-text vector per row, so it never
        # runs over every matching chat.
        candidates = (f"SELECT cs.id FROM chat_sessions cs WHERE {where} "
                      "ORDER BY cs.updated_at DESC, cs.id DESC LIMIT %s")
        cand_params = params + [SEARCH_CANDIDATES]
        cur.execute(f"SELECT COUNT(*) FROM ({candidates}) c", tuple(cand_params))
        total = cur.fetchone()[0] or 0
        search_capped = total >= SEARCH_CANDIDATES
        phrase = phrase_of(f.query)
        order, order_params = "", []
        if phrase:
            order = f"({SEARCH_DOC_SQL} @@ phraseto_tsquery('simple', %s)) DESC, "
            order_params = [phrase]
        cur.execute(
            f"""WITH cand AS ({candidates})
                SELECT {_CHAT_COLUMNS}
                FROM chat_sessions cs JOIN cand ON cand.id = cs.id
                LEFT JOIN users u ON u.id = cs.user_id
                WHERE cs.org_id = %s
                ORDER BY {order}cs.updated_at DESC, cs.id DESC
                LIMIT %s OFFSET %s""",
            tuple(cand_params + [org_id] + order_params + [f.per_page, offset]),
        )
    else:
        cur.execute(f"SELECT COUNT(*) FROM chat_sessions cs WHERE {where}", tuple(params))
        total = cur.fetchone()[0] or 0
        cur.execute(
            f"""SELECT {_CHAT_COLUMNS}
                FROM chat_sessions cs LEFT JOIN users u ON u.id = cs.user_id
                WHERE {where}
                ORDER BY cs.updated_at DESC, cs.id DESC
                LIMIT %s OFFSET %s""",
            tuple(params + [f.per_page, offset]),
        )
    rows = fetch_dicts(cur)
    ids = [r["id"] for r in rows]

    issues, snippets = {}, {}
    if ids:
        issues = _issue_counts_for(cur, org_id, ids, f.period)
        if f.query:
            snippets = _snippets_for(cur, org_id, ids, f.query)
            # A chat that only matched inside a redacted secret is not a match.
            hidden = [i for i in ids if snippets.get(i) is None]
            rows = [r for r in rows if r["id"] not in hidden]
            total = max(0, total - len(hidden))

    chats = []
    for r in rows:
        by_cat = issues.get(r["id"], {})
        match = snippets.get(r["id"])
        chats.append({
            **_chat_summary(r),
            "issues": {cat: n for cat, n in by_cat.items() if cat != OTHER},
            "failed_tool_calls": sum(by_cat.values()),
            "match": match if match and match.get("text") else None,
            # "exact": one message contains the phrase; "all_words": the words
            # appear, possibly spread across messages.
            **({"match_type": match["match_type"]} if f.query else {}),
        })

    counts_where, counts_params = _chat_where(f, org_id, skip=("status", "issue", "query"))
    return {
        "chats": chats,
        "total": total,
        "search_capped": search_capped,
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


def get_transcript(cur, org_id: str, session_id: str, *, tail: bool = False) -> Optional[dict]:
    """A chat in the chat UI's message shape, with detected issues; None if not in this org.

    Loads at most ``MAX_TRANSCRIPT_MESSAGES`` messages: the first ones (the page,
    which reads from the top), or with ``tail`` the last ones (the agent, which
    reports the latest messages). Message indexes are always the real ones.
    """
    validate_session_id(session_id)
    first = (f"GREATEST({_MESSAGE_COUNT} - {MAX_TRANSCRIPT_MESSAGES}, 0)" if tail else "0")
    cur.execute(
        f"""SELECT {_CHAT_COLUMNS}, {first} AS first_index,
                   COALESCE((SELECT jsonb_agg(e.m ORDER BY e.idx)
                             FROM jsonb_array_elements({_MESSAGES_ARRAY}) WITH ORDINALITY AS e(m, idx)
                             WHERE e.idx > {first} AND e.idx <= {first} + {MAX_TRANSCRIPT_MESSAGES}),
                            '[]'::jsonb) AS messages
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
    for index, raw in enumerate(row.get("messages") or [], start=row.get("first_index") or 0):
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
