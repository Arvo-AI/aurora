"""Org-wide chat audit service: filters, scoping, search, issues, transcripts."""

import json
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from services.monitor import chat_audit as ca
from services.monitor.chat_audit import ChatFilters, InvalidChatFilter


def _assert_params_align(cur):
    """Every executed query binds exactly as many params as it has placeholders."""
    for call in cur.execute.call_args_list:
        sql, params = call.args
        assert sql.count("%s") == len(params), sql


class Cursor:
    """Fake cursor that answers queries in order from a script of (description, rows)."""

    def __init__(self, script):
        self.script = list(script)
        self.execute = MagicMock(side_effect=self._execute)
        self.description = None
        self._rows = []

    def _execute(self, sql, params=()):
        cols, rows = self.script.pop(0) if self.script else (None, [])
        self.description = [(c,) for c in cols] if cols else None
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


# --- filters ----------------------------------------------------------------

def test_defaults():
    f = ChatFilters.parse({})
    assert (f.period, f.source, f.status, f.issue, f.page, f.per_page) == ("7d", None, None, None, 1, 25)


@pytest.mark.parametrize("raw", [
    {"period": "1y"}, {"source": "email"}, {"status": "weird"}, {"issue": "nope"},
    {"user": "x" * 300},
])
def test_invalid_filters(raw):
    with pytest.raises(InvalidChatFilter) as exc:
        ChatFilters.parse(raw)
    assert ca.chat_filter_error_message(exc.value) in ca.CHAT_FILTER_ERRORS.values()


def test_all_means_unfiltered_and_paging_is_capped():
    f = ChatFilters.parse({"source": "all", "issue": "all", "per_page": "999", "page": "0"})
    assert f.source is None and f.issue is None
    assert (f.page, f.per_page) == (1, 100)


@pytest.mark.parametrize("sid, ok", [
    ("2f9a-session_1", True), ("abc::sa_2", True), ("../etc", False), ("", False), ("x" * 65, False),
])
def test_session_id_validation(sid, ok):
    if ok:
        assert ca.validate_session_id(sid) == sid
    else:
        with pytest.raises(InvalidChatFilter):
            ca.validate_session_id(sid)


# --- WHERE ------------------------------------------------------------------

def test_where_is_org_scoped_and_hides_deleted_chats():
    sql, params = ca._chat_where(ChatFilters.parse({}), "org-1")
    assert sql.startswith("cs.org_id = %s AND cs.is_active = true")
    assert params[:2] == ["org-1", "7 days"]


def test_where_uses_the_indexed_search_expression():
    sql, params = ca._chat_where(ChatFilters.parse({"q": "AccessDenied"}), "org-1")
    assert ca.SEARCH_DOC_SQL in sql
    assert "websearch_to_tsquery('simple', %s)" in sql
    assert params[-1] == "AccessDenied"


def test_search_expression_matches_the_index():
    # Postgres only uses the GIN index when the query expression is identical.
    assert ca.SEARCH_DOC_SQL.replace("cs.messages", "messages") in ca.SEARCH_INDEX_SQL


def test_issue_filter_binds_patterns_and_category():
    sql, params = ca._chat_where(ChatFilters.parse({"issue": "missing_permission", "source": "slack"}), "o")
    assert sql.count("%s") == len(params)
    assert params[-1] == "missing_permission"
    assert "EXISTS (SELECT 1 FROM execution_steps es" in sql
    assert "es.org_id = cs.org_id" in sql


def test_any_issue_filter():
    sql, params = ca._chat_where(ChatFilters.parse({"issue": "any"}), "o")
    assert "es.status = 'error')" in sql and sql.count("%s") == len(params)


# --- list -------------------------------------------------------------------

_LIST_COLS = ["id", "title", "user_id", "user_email", "user_name", "source", "trigger_source",
              "created_at", "updated_at", "status", "incident_id", "message_count"]


def _list_script(rows, issue_rows=(), snippet_rows=None):
    ts = datetime(2026, 11, 9, 10, 0)
    script = [
        (["count"], [(len(rows),)]),
        (_LIST_COLS, [(*r[:7], ts, ts, *r[7:]) for r in rows]),
        (["sid", "category", "count"], list(issue_rows)),
    ]
    if snippet_rows is not None:
        script.append((["id", "msg_index", "sender", "text"], snippet_rows))
    script += [
        (["category", "calls", "chats"], [("missing_permission", 12, 3), ("other", 2, 1)]),
        (["source", "count"], [("slack", 4), ("web", 9)]),
        (["user_id", "email", "name", "count"], [("u1", "a@x.io", "A", 5)]),
    ]
    return script


def test_list_shapes_rows_badges_and_facets():
    cur = Cursor(_list_script(
        [("s1", "Why is checkout slow", "u1", "a@x.io", "A", "slack", "slack", "completed", None, 6)],
        issue_rows=[("s1", "missing_permission", 2), ("s1", "other", 1)],
    ))
    out = ca.list_chats(cur, "org-1", ChatFilters.parse({}))

    chat = out["chats"][0]
    assert chat["source"] == "slack" and chat["user"]["email"] == "a@x.io"
    assert chat["issues"] == {"missing_permission": 2}
    assert chat["failed_tool_calls"] == 3
    assert chat["match"] is None

    totals = {c["category"]: c for c in out["issue_counts"]}
    assert totals["missing_permission"]["calls"] == 12 and totals["missing_permission"]["chats"] == 3
    assert totals["blocked_command"]["calls"] == 0
    assert {s["source"]: s["count"] for s in out["sources"]}["slack"] == 4
    _assert_params_align(cur)
    for call in cur.execute.call_args_list:
        assert "org-1" in call.args[1]


def test_list_search_returns_redacted_snippet_with_message_anchor():
    text = "Tried again with --password hunter2 and got AccessDenied from IAM"
    cur = Cursor(_list_script(
        [("s1", "t", "u1", None, None, "web", "", "completed", None, 2)],
        snippet_rows=[("s1", 1, "bot", text)],
    ))
    out = ca.list_chats(cur, "org-1", ChatFilters.parse({"q": "AccessDenied"}))
    match = out["chats"][0]["match"]
    assert match["message_index"] == 1 and match["sender"] == "aurora"
    assert "AccessDenied" in match["text"]
    assert "hunter2" not in match["text"]
    _assert_params_align(cur)


def test_list_empty_page_skips_per_chat_queries():
    cur = Cursor([(["count"], [(0,)]), (_LIST_COLS, []),
                  (["category", "calls", "chats"], []), (["source", "count"], []),
                  (["user_id", "email", "name", "count"], [])])
    out = ca.list_chats(cur, "org-1", ChatFilters.parse({"q": "x"}))
    assert out["chats"] == [] and out["total_pages"] == 1
    assert cur.execute.call_count == 5


# --- transcript -------------------------------------------------------------

_T_COLS = ["id", "title", "user_id", "email", "name", "source", "trigger_source", "status",
           "created_at", "updated_at", "incident_id", "message_count", "messages"]
_S_COLS = ["tool_call_id", "tool_name", "tool_input", "status", "error_message", "started_at"]


def _transcript(messages, steps=()):
    ts = datetime(2026, 11, 9, 10, 0)
    cur = Cursor([
        (_T_COLS, [("s1", "t", "u1", "a@x.io", "A", "web", "", "completed", ts, ts, None,
                    len(messages), messages)]),
        (_S_COLS, list(steps)),
    ])
    return ca.get_transcript(cur, "org-1", "s1"), cur


def test_transcript_not_found_in_org():
    cur = Cursor([(_T_COLS, [])])
    assert ca.get_transcript(cur, "org-2", "s1") is None
    sql, params = cur.execute.call_args.args
    assert "cs.org_id = %s" in sql and params == ("s1", "org-2")


def test_transcript_classifies_tool_calls_and_replies_and_redacts():
    ts = datetime(2026, 11, 9, 10, 0)
    messages = [
        {"sender": "user", "text": "list instances, aws_access_key_id = AKIAZ7Q4R2M8N5K3P1W6"},
        {"sender": "bot", "text": "Checking.", "toolCalls": [
            {"id": "tc1", "tool_name": "cloud_exec",
             "input": json.dumps({"provider": "aws", "command": "ec2 describe-instances"}),
             "output": '{"error": "AccessDenied: not authorized to perform ec2:DescribeInstances"}'},
            {"id": "tc2", "tool_name": "cloud_exec",
             "input": json.dumps({"command": "ec2 terminate-instances --instance-ids i-1"}),
             "output": '{"error": "blocked", "code": "READ_ONLY_MODE"}'},
            {"id": "tc3", "tool_name": "kubectl", "input": "{}", "output": "pod-a Running"},
        ]},
        {"sender": "bot", "text": "I don't have the required IAM permissions to list instances."},
    ]
    steps = [("tc1", "cloud_exec", {}, "error", '{"error": "AccessDenied"}', ts),
             ("sub-1", "terminal_exec", {"command": "helm ls"}, "error", "helm: command not found", ts)]
    out, cur = _transcript(messages, steps)

    assert "AKIAZ7Q4R2M8N5K3P1W6" not in out["messages"][0]["text"]
    calls = out["messages"][1]["tool_calls"]
    assert [c["issue"] for c in calls] == ["missing_permission", "blocked_command", None]
    assert calls[0]["status"] == "failed" and calls[0]["input"] == "aws ec2 describe-instances"
    assert calls[2]["status"] != "failed"
    assert out["messages"][2]["issue"] == "missing_permission"

    anchors = [(i["category"], i["message_index"]) for i in out["issues"]]
    assert ("missing_permission", 1) in anchors
    assert ("blocked_command", 1) in anchors
    assert ("missing_permission", 2) in anchors
    assert ("unavailable", None) in anchors          # sub-agent failure not in the transcript
    assert out["other_failed_tool_calls"][0]["tool_name"] == "terminal_exec"

    step_sql, step_params = cur.execute.call_args_list[1].args
    assert "es.org_id = %s" in step_sql and step_params == ("org-1", "s1")
    _assert_params_align(cur)


def test_transcript_tolerates_malformed_messages():
    out, _ = _transcript(["junk", {"sender": "bot", "text": None, "toolCalls": ["bad"]}])
    assert len(out["messages"]) == 1 and out["messages"][0]["tool_calls"] == []
