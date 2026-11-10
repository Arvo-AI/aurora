"""Tool-call search shared by the monitor page and the agent."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from services.monitor.tool_call_search import (
    InvalidFilter,
    ToolCallFilters,
    _where,
    failure_reason,
    scrub,
    search_tool_calls,
)


# --- filter parsing ---------------------------------------------------------

def test_defaults_to_last_week_all_statuses():
    f = ToolCallFilters.parse({})
    assert f.period == "7d"
    assert f.status is None
    assert (f.page, f.per_page) == (1, 25)


@pytest.mark.parametrize("raw", [
    {"status": "broken"},
    {"period": "1y"},
    {"source": "slack"},
    {"since": "not-a-date"},
    {"since": "2026-11-10T00:00:00Z", "until": "2026-11-01T00:00:00Z"},
])
def test_invalid_filters_are_rejected(raw):
    with pytest.raises(InvalidFilter):
        ToolCallFilters.parse(raw)


def test_paging_is_clamped_and_garbage_falls_back():
    f = ToolCallFilters.parse({"page": "-3", "per_page": "5000"})
    assert (f.page, f.per_page) == (1, 100)
    f = ToolCallFilters.parse({"page": "abc", "per_page": "x"})
    assert (f.page, f.per_page) == (1, 25)


def test_all_means_no_filter():
    f = ToolCallFilters.parse({"status": "all", "source": "all"})
    assert f.status is None and f.source is None


# --- WHERE clause -----------------------------------------------------------

def test_where_always_scopes_to_org():
    sql, params = _where(ToolCallFilters.parse({}), "org-1")
    assert sql.startswith("es.org_id = %s")
    assert params[0] == "org-1"
    assert params[1] == "7 days"


def test_where_maps_failed_to_error_status():
    sql, params = _where(ToolCallFilters.parse({"status": "failed"}), "org-1")
    assert "es.status = %s" in sql
    assert "error" in params


def test_since_until_override_period():
    f = ToolCallFilters.parse({"since": "2026-11-01T00:00:00Z", "until": "2026-11-08T00:00:00Z"})
    sql, params = _where(f, "org-1")
    assert "interval" not in sql
    assert params[1] == datetime(2026, 11, 1, tzinfo=timezone.utc)


def test_free_text_escapes_like_wildcards():
    sql, params = _where(ToolCallFilters.parse({"q": "50%_off"}), "org-1")
    assert "ILIKE" in sql
    assert params[-1] == "%50\\%\\_off%"


def test_source_filter():
    chat_sql, _ = _where(ToolCallFilters.parse({"source": "chat"}), "o")
    inc_sql, _ = _where(ToolCallFilters.parse({"source": "incident"}), "o")
    assert "es.incident_id IS NULL" in chat_sql
    assert "es.incident_id IS NOT NULL" in inc_sql


def test_facet_queries_skip_their_own_filter():
    f = ToolCallFilters.parse({"status": "failed", "tool": "cloud_exec"})
    sql, params = _where(f, "o", skip=("status", "tool"))
    assert "es.status" not in sql and "es.tool_name = %s" not in sql
    assert "error" not in params and "cloud_exec" not in params


# --- redaction / reason -----------------------------------------------------

@pytest.mark.parametrize("text, secret", [
    ("mysql -u root --password hunter2 -e 'select 1'", "hunter2"),
    ("curl --token=abc123def https://x", "abc123def"),
    ('{"api_key": "sk-live-abcdef"}', "sk-live-abcdef"),
    ("password=swordfish connection refused", "swordfish"),
])
def test_scrub_removes_flagged_secrets(text, secret):
    out = scrub(text)
    assert secret not in out
    assert "[REDACTED]" in out


def test_scrub_keeps_ordinary_commands():
    cmd = "kubectl get pods -n payments"
    assert scrub(cmd) == cmd


def test_reason_extracts_json_error():
    body = '{"error": "AccessDenied: User is not authorized to perform ec2:DescribeInstances", "code": 403}'
    assert failure_reason(body).startswith("AccessDenied")


def test_reason_uses_first_non_blank_line():
    assert failure_reason("\n\nError from server (Forbidden): pods is forbidden\nmore") == \
        "Error from server (Forbidden): pods is forbidden"


def test_reason_handles_errors_list_and_empty():
    assert failure_reason('{"errors": ["rate limited"]}') == "rate limited"
    assert failure_reason(None) == ""


# --- full search against a fake cursor -------------------------------------

_COLS = ["id", "tool_name", "tool_input", "status", "error_message", "started_at",
         "completed_at", "duration_ms", "session_id", "incident_id",
         "root_session_id", "incident_title", "chat_title"]


def _cursor(rows, status_counts, tool_counts, total):
    cur = MagicMock()
    cur.description = [(c,) for c in _COLS]
    cur.fetchone.return_value = (total,)
    cur.fetchall.side_effect = [rows, status_counts, tool_counts]
    return cur


def test_search_shapes_rows_links_and_facets():
    ts = datetime(2026, 11, 5, 12, 0)
    rows = [
        (1, "cloud_exec", {"provider": "aws", "command": "ec2 describe-instances"},
         "error", '{"error": "AccessDenied: not authorized"}', ts, ts, 120,
         "sess-1::sa_2", None, "sess-1", None, "Why is checkout slow"),
        (2, "terminal_exec", {"command": "kubectl get pods --token=s3cr3t"},
         "success", None, ts, ts, 40, "sess-9", "11111111-1111-1111-1111-111111111111",
         "sess-9", "High latency", None),
    ]
    cur = _cursor(rows, [("error", 3), ("success", 7)], [("cloud_exec", 5, 3)], total=2)

    out = search_tool_calls(cur, "org-1", ToolCallFilters.parse({"per_page": "10"}))

    failed, ok = out["items"]
    assert failed["status"] == "failed"
    assert failed["input_summary"] == "aws ec2 describe-instances"
    assert failed["reason"] == "AccessDenied: not authorized"
    assert failed["started_at"].endswith("+00:00")
    # Sub-agent rows link to the parent chat the user can open
    assert failed["source"] == {"type": "chat", "id": "sess-1",
                                "title": "Why is checkout slow", "url": "/chat?sessionId=sess-1"}

    assert ok["status"] == "succeeded" and ok["reason"] == "" and ok["error"] == ""
    assert "s3cr3t" not in ok["input_summary"]
    assert ok["source"]["url"] == "/incidents/11111111-1111-1111-1111-111111111111"

    assert out["summary"] == {"total": 10, "failed": 3, "succeeded": 7, "running": 0}
    assert out["tools"] == [{"tool_name": "cloud_exec", "total": 5, "failed": 3}]
    assert out["total_pages"] == 1

    # Every query is org-scoped
    for call in cur.execute.call_args_list:
        assert "es.org_id = %s" in call.args[0]
        assert call.args[1][0] == "org-1"


def test_search_pages_with_offset():
    cur = _cursor([], [], [], total=55)
    out = search_tool_calls(cur, "org-1", ToolCallFilters.parse({"page": "3", "per_page": "25"}))
    page_params = cur.execute.call_args_list[1].args[1]
    assert page_params[-2:] == (25, 50)
    assert out["total_pages"] == 3
