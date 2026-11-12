"""Classify failed tool calls and agent replies into actionable issue types.

The chat audit view surfaces what an admin can fix: a permission Aurora lacks,
a command a gate refused, a connector that isn't connected. One ordered table
drives every place this runs:

* Python (``classify_tool_error`` / ``classify_reply``) for the transcript view,
* SQL (``sql_category_case``) for per-period counts and the issue filter, which
  run over ``execution_steps.error_message`` with Postgres ``~*``.

Structured signals win: the command gate and tools put a machine ``code`` in
their error JSON (``"code": "READ_ONLY_MODE"``), matched before any free-text
pattern. Text patterns are the fallback, and agent *replies* only get a narrow
set of them, since a reply can quote an error while discussing something else.

Patterns must stay valid in both Python ``re`` and Postgres ARE (POSIX) regex:
no ``\\b``, lookarounds or inline flags. Matching is case-insensitive in both.
"""

from __future__ import annotations

import re
from typing import Optional

MISSING_PERMISSION = "missing_permission"
BLOCKED_COMMAND = "blocked_command"
NOT_CONNECTED = "not_connected"
TIMEOUT = "timeout"
UNAVAILABLE = "unavailable"
OTHER = "other"

CATEGORY_LABELS = {
    MISSING_PERMISSION: "Missing permission",
    BLOCKED_COMMAND: "Blocked command",
    NOT_CONNECTED: "Not connected / expired credentials",
    TIMEOUT: "Timeout",
    UNAVAILABLE: "Command unavailable",
    OTHER: "Other failure",
}

# Categories an admin filters on, in display order (OTHER is counted, not filtered).
ISSUE_CATEGORIES = (MISSING_PERMISSION, BLOCKED_COMMAND, NOT_CONNECTED, TIMEOUT, UNAVAILABLE)

# Machine codes the command gate / tools emit in their error JSON.
_GATE_CODES = (
    "READ_ONLY_MODE|POLICY_DENIED|SAFETY_BLOCKED|SIGNATURE_MATCHED|"
    "USER_DECLINED|BACKGROUND_DENIED|TOOL_NOT_ALLOWED|ACTION_CONFIRM"
)

# Ordered (category, pattern) table — first match wins. Structured codes first.
TOOL_ERROR_PATTERNS: tuple[tuple[str, str], ...] = (
    # --- structured codes ----------------------------------------------------
    (BLOCKED_COMMAND, r'"code" *: *"(' + _GATE_CODES + r')"'),
    (MISSING_PERMISSION, r'"code" *: *"CONNECTOR_DENIED"'),
    # --- gate / guardrail wording -------------------------------------------
    (BLOCKED_COMMAND, r"blocked in ask mode|in agent mode to proceed|switch to agent mode"),
    (BLOCKED_COMMAND, r"blocked by (the )?(command )?(policy|guardrail|safety)|user declined|declined by (the )?user"),
    # --- connectors ----------------------------------------------------------
    (NOT_CONNECTED, r"not connected|no (valid )?credentials (found|configured)|please connect|"
                    r"token (may be |has |is )?expired|expired token|credentials (have )?expired|"
                    r"invalid_grant|re-?authenticate|reconnect|"
                    # kubectl with no reachable or configured cluster
                    r"connection refused|connection to the server .* was refused|"
                    r"current-context is not set|no configuration has been provided"),
    # --- permissions ---------------------------------------------------------
    (MISSING_PERMISSION, r"accessdenied|access denied|unauthorizedoperation|authorizationfailed|"
                         r"is not authorized to perform|not authorized|permission denied|"
                         r"permission_denied|insufficient (permission|privilege|scope)|"
                         r"does not have (the )?(required )?permission|forbidden|"
                         r"(^|[^0-9])403([^0-9]|$)|iam permission"),
    # --- timeouts ------------------------------------------------------------
    # Phrases only: the bare word appears in healthy text, e.g. kubectl's
    # "GET .../api?timeout=32s" on a connection-refused error.
    (TIMEOUT, r"timed out|deadline exceeded|timeout ?expired|i/o timeout|"
              r"(request|gateway|read|connect|connection) time-?out"),
    # --- command unavailable -------------------------------------------------
    (UNAVAILABLE, r"command not found|not installed|executable file not found|"
                  r"no such file or directory|unknown command|is not recognized as"),
)

# Narrow fallback for agent replies: phrases Aurora uses when it *couldn't* do something.
REPLY_PATTERNS: tuple[tuple[str, str], ...] = (
    (BLOCKED_COMMAND, r"blocked in ask mode|switch to agent mode|in agent mode to proceed|"
                      r"(was|is|were) blocked by (the )?(command )?(policy|guardrail|safety)"),
    (MISSING_PERMISSION, r"accessdenied|access denied|is not authorized to perform|"
                         r"(lack|lacks|missing|don't have|do not have|doesn't have|does not have) "
                         r"(the )?(required |necessary )?(iam |rbac )?permissions?"),
    (NOT_CONNECTED, r"(isn't|is not|not|no longer) connected|credentials (have )?expired|"
                    r"token (has )?expired"),
)

# The leading entries that are gate codes or gate wording: reliable on any output.
_STRUCTURED_ENTRIES = 4

_TOOL_RE = tuple((cat, re.compile(p, re.IGNORECASE)) for cat, p in TOOL_ERROR_PATTERNS)
_REPLY_RE = tuple((cat, re.compile(p, re.IGNORECASE)) for cat, p in REPLY_PATTERNS)

# Postgres regex can't be bounded per-row cheaply; classify only the head of an error.
SQL_SCAN_CHARS = 4000


def classify_tool_error(text: Optional[str]) -> str:
    """Category for a failed tool call's error/output. ``OTHER`` if nothing matches."""
    if not text:
        return OTHER
    head = text[:SQL_SCAN_CHARS]
    for category, pattern in _TOOL_RE:
        if pattern.search(head):
            return category
    return OTHER


def classify_tool_output(text: Optional[str]) -> Optional[str]:
    """Category for a tool output that may or may not be an error.

    Used on transcript tool calls whose status isn't recorded: only the
    structured gate codes and connector messages count, so an output that merely
    *contains* "403" (a log line, a metric) isn't flagged.
    """
    if not text:
        return None
    head = text[:SQL_SCAN_CHARS]
    for category, pattern in _TOOL_RE[:_STRUCTURED_ENTRIES]:
        if pattern.search(head):
            return category
    if re.search(r'^\s*\{\s*"(error|errors)"\s*:', head) or head.lstrip().lower().startswith("error"):
        category = classify_tool_error(head)
        return None if category == OTHER else category
    return None


def classify_reply(text: Optional[str]) -> Optional[str]:
    """Category for an agent reply that reports it couldn't do something, else None."""
    if not text:
        return None
    for category, pattern in _REPLY_RE:
        if pattern.search(text):
            return category
    return None


def sql_category_case(column: str = "es.error_message") -> tuple[str, list[str]]:
    """``CASE`` expression classifying ``column`` in SQL, plus its params.

    Patterns are bound as parameters (never interpolated) so the table can't
    inject SQL. Mirrors ``classify_tool_error`` exactly.
    """
    head = f"LEFT(COALESCE({column}, ''), {SQL_SCAN_CHARS})"
    whens = " ".join(f"WHEN {head} ~* %s THEN '{cat}'" for cat, _ in TOOL_ERROR_PATTERNS)
    return f"(CASE {whens} ELSE '{OTHER}' END)", [p for _, p in TOOL_ERROR_PATTERNS]
