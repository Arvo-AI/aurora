"""Chat audit query layer: filters, org scoping, search, failures, transcripts, redaction."""

import json
from datetime import datetime

import pytest

from services.monitor import chat_audit as ca
from services.monitor.chat_audit import ChatFilters, InvalidFilter

TS = datetime(2026, 11, 9, 10, 0)


class Cursor:
    """Fake cursor answering each query from ``routes``: [(sql substring, columns, rows)]."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.description = None
        self._rows = []

    def execute(self, sql, params=()):
        assert sql.count("%s") == len(params), sql  # placeholders and params line up
        self.calls.append((sql, tuple(params)))
        for needle, cols, rows in self.routes:
            if needle in sql:
                break
        else:
            cols, rows = ["unused"], []
        self.description = [(c,) for c in cols] if cols else None
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


# --- redaction and reasons --------------------------------------------------

@pytest.mark.parametrize("text, secret", [
    ("mysql -u root --password hunter2 -e 'select 1'", "hunter2"),
    ("curl --token=abc123def https://x", "abc123def"),
    ('{"api_key": "sk-live-abcdef"}', "sk-live-abcdef"),
    ("password=swordfish connection refused", "swordfish"),
])
def test_scrub_removes_secrets(text, secret):
    assert secret not in ca.scrub(text)


def test_scrub_keeps_ordinary_commands():
    assert ca.scrub("kubectl get pods -n payments") == "kubectl get pods -n payments"


def test_scrub_json_text_redacts_values_and_stays_valid_json():
    raw = json.dumps({"command": "psql --password hunter2", "api_key": "abc", "nested": [{"token": "t"}]})
    out = ca.scrub_json_text(raw)
    parsed = json.loads(out)  # the chat's tool widgets parse this per tool
    assert "hunter2" not in out and parsed["api_key"] == "[REDACTED]"
    assert parsed["nested"][0]["token"] == "[REDACTED]"
    assert ca.scrub_json_text("plain --password hunter2") == "plain --password [REDACTED]"


def test_failure_reason():
    assert ca.failure_reason('{"error": "AccessDenied: no", "code": 403}') == "AccessDenied: no"
    assert ca.failure_reason("\n\nError from server (Forbidden): x\nmore") == "Error from server (Forbidden): x"
    assert ca.failure_reason('{"errors": ["rate limited"]}') == "rate limited"
    assert ca.failure_reason(None) == ""


# --- filters ----------------------------------------------------------------

def test_defaults():
    f = ChatFilters.parse({})
    assert (f.period, f.source, f.status, f.issue, f.page, f.per_page) == ("7d", None, None, None, 1, 25)


@pytest.mark.parametrize("raw", [
    {"period": "1y"}, {"source": "email"}, {"status": "weird"}, {"issue": "nope"}, {"user": "x" * 300},
])
def test_invalid_filters_have_constant_messages(raw):
    with pytest.raises(InvalidFilter) as exc:
        ChatFilters.parse(raw)
    assert ca.filter_error_message(exc.value) in ca.FILTER_ERRORS.values()


def test_all_means_unfiltered_and_paging_is_capped():
    f = ChatFilters.parse({"source": "all", "issue": "all", "per_page": "999", "page": "0"})
    assert f.source is None and f.issue is None and (f.page, f.per_page) == (1, 100)
    assert ChatFilters.parse({"issue": "other"}).issue == "other"


@pytest.mark.parametrize("sid, ok", [
    ("2f9a-session_1", True), ("abc::sa_2", True), ("../etc", False), ("", False), ("x" * 65, False),
])
def test_session_id_validation(sid, ok):
    if ok:
        assert ca.validate_session_id(sid) == sid
    else:
        with pytest.raises(InvalidFilter):
            ca.validate_session_id(sid)


# --- WHERE ------------------------------------------------------------------

def test_where_is_org_scoped_and_hides_deleted_chats():
    sql, params = ca._chat_where(ChatFilters.parse({}), "org-1")
    assert sql.startswith("cs.org_id = %s AND cs.is_active = true")
    assert params[:2] == ["org-1", "7 days"]


def test_search_uses_the_indexed_expression():
    sql, params = ca._chat_where(ChatFilters.parse({"q": "AccessDenied"}), "org-1")
    assert ca.SEARCH_DOC_SQL in sql and params[-1] == "AccessDenied"
    # Postgres only uses the GIN index when the query expression is identical.
    assert ca.SEARCH_DOC_SQL.replace("cs.messages", "messages") in ca.SEARCH_INDEX_SQL


def test_issue_filter_binds_patterns_and_category():
    sql, params = ca._chat_where(ChatFilters.parse({"issue": "missing_permission", "source": "slack"}), "o")
    assert sql.count("%s") == len(params) and params[-1] == "missing_permission"
    assert "es.org_id = cs.org_id" in sql


# --- list -------------------------------------------------------------------

_CHAT_COLS = ["id", "title", "user_id", "user_email", "user_name", "source", "trigger_source",
              "status", "created_at", "updated_at", "incident_id", "message_count"]
_FAIL_COLS = ["id", "tool_name", "tool_input", "error_message", "started_at", "tool_call_id",
              "incident_id", "chat_id", "rn", "message_index"]


def _chat_row(sid="s1", source="slack"):
    return (sid, "Why is checkout slow", "u1", "a@x.io", "A", source, source, "completed", TS, TS, None, 6)


def test_list_shapes_rows_badges_failures_and_facets():
    cur = Cursor([
        ("SELECT COUNT(*) FROM chat_sessions", ["count"], [(1,)]),
        ("ORDER BY cs.updated_at DESC", _CHAT_COLS, [_chat_row()]),
        ("GROUP BY 1, 2", ["sid", "category", "count"], [("s1", "missing_permission", 2), ("s1", "other", 1)]),
        ("ROW_NUMBER()", _FAIL_COLS, [
            (7, "cloud_exec", {"provider": "aws", "command": "ec2 describe-instances"},
             '{"error": "AccessDenied: not authorized"}', TS, "tc1", None, "s1", 1, 3),
        ]),
        ("COUNT(DISTINCT", ["category", "calls", "chats"], [("missing_permission", 12, 3)]),
        ("GROUP BY cs.user_id", ["uid", "email", "name", "n"], [("u1", "a@x.io", "A", 5)]),
        ("FROM chat_sessions cs WHERE", ["source", "count"], [("slack", 4)]),
    ])
    out = ca.list_chats(cur, "org-1", ChatFilters.parse({"issue": "any"}))

    chat = out["chats"][0]
    assert chat["source"] == "slack" and chat["user"]["email"] == "a@x.io"
    assert chat["issues"] == {"missing_permission": 2} and chat["failed_tool_calls"] == 3
    failure = chat["failures"][0]
    assert failure["message_index"] == 3 and failure["tool_call_id"] == "tc1"
    assert failure["input"] == "aws ec2 describe-instances" and failure["category"] == "missing_permission"
    assert failure["reason"] == "AccessDenied: not authorized"

    totals = {c["category"]: c for c in out["issue_counts"]}
    assert totals["missing_permission"]["calls"] == 12 and totals["blocked_command"]["calls"] == 0
    assert {s["source"]: s["count"] for s in out["sources"]}["slack"] == 4
    for sql, params in cur.calls:  # every query is scoped to the caller's org
        assert "org-1" in params, sql


def test_failures_are_capped_per_chat_before_the_overall_limit():
    cur = Cursor([])
    ca._failed_tool_calls(cur, "org-1", chat_ids=["a", "b"], per_chat=5, limit=10)
    sql, params = cur.calls[0]
    assert sql.index("ranked.rn <= %s") < sql.index("LIMIT %s\n")
    assert params[-2:] == (5, 10)


def test_list_search_returns_redacted_snippet_with_message_anchor():
    text = "Tried again with --password hunter2 and got AccessDenied from IAM"
    cur = Cursor([
        ("SELECT COUNT(*) FROM chat_sessions", ["count"], [(1,)]),
        ("ORDER BY cs.updated_at DESC", _CHAT_COLS, [_chat_row(source="web")]),
        ("JOIN LATERAL", ["id", "msg_index", "sender", "text"], [("s1", 1, "bot", text)]),
    ])
    match = ca.list_chats(cur, "org-1", ChatFilters.parse({"q": "AccessDenied"}))["chats"][0]["match"]
    assert match["message_index"] == 1 and match["sender"] == "aurora"
    assert "AccessDenied" in match["text"] and "hunter2" not in match["text"]


# --- transcript -------------------------------------------------------------

def _transcript(messages, steps=(), failures=()):
    cur = Cursor([
        ("jsonb_path_query_array", _CHAT_COLS + ["messages"],
         [(*_chat_row(source="web"), messages)]),
        ("SELECT tool_call_id, status, error_message", ["tool_call_id", "status", "error_message"], list(steps)),
        ("ROW_NUMBER()", _FAIL_COLS, list(failures)),
    ])
    return ca.get_transcript(cur, "org-1", "s1"), cur


def test_transcript_not_found_in_org():
    cur = Cursor([])
    assert ca.get_transcript(cur, "org-2", "s1") is None
    sql, params = cur.calls[0]
    assert "cs.org_id = %s" in sql and params == ("s1", "org-2")


def test_transcript_uses_chat_message_shape_classifies_and_redacts():
    messages = [
        {"sender": "user", "text": "list instances, then run psql --password hunter2"},
        {"sender": "bot", "text": "Checking.", "toolCalls": [
            {"id": "tc1", "tool_name": "cloud_exec",
             "input": json.dumps({"provider": "aws", "command": "ec2 describe-instances --password p4ss"}),
             "output": '{"error": "AccessDenied: not authorized to perform ec2:DescribeInstances"}'},
            {"id": "tc2", "tool_name": "cloud_exec", "input": "{}",
             "output": '{"error": "blocked", "code": "READ_ONLY_MODE"}'},
            {"id": "tc3", "tool_name": "kubectl", "input": "{}", "output": "pod-a Running"},
        ]},
        {"sender": "bot", "text": "I don't have the required IAM permissions to list instances."},
    ]
    failures = [(9, "terminal_exec", {"command": "helm ls"}, "helm: command not found", TS, None, None, "s1", 1, None)]
    out, cur = _transcript(messages, steps=[("tc1", "error", '{"error": "AccessDenied"}')], failures=failures)

    first, bot, reply = out["messages"]
    assert (first["id"], first["sender"], bot["sender"]) == (0, "user", "bot")
    assert "hunter2" not in first["text"]

    calls = bot["toolCalls"]
    assert [c["status"] for c in calls] == ["error", "error", "completed"]
    assert "p4ss" not in calls[0]["input"] and json.loads(calls[0]["input"])["provider"] == "aws"
    assert calls[0]["error"].startswith("AccessDenied")

    anchors = {(i["category"], i["message_index"]) for i in out["issues"]}
    assert {("missing_permission", 1), ("blocked_command", 1), ("missing_permission", 2),
            ("unavailable", None)} <= anchors          # last one: a sub-agent call not in the transcript
    for sql, params in cur.calls:
        assert "org-1" in params, sql


def test_transcript_tolerates_malformed_messages():
    out, _ = _transcript(["junk", {"sender": "bot", "text": None, "toolCalls": ["bad"]}])
    assert len(out["messages"]) == 1 and out["messages"][0]["toolCalls"] == []


# --- agent tool entry point -------------------------------------------------

def test_search_failed_tool_calls_filters_and_links():
    cur = Cursor([
        ("ROW_NUMBER()", _FAIL_COLS, [
            (1, "cloud_exec", {"command": "s3 ls"}, "AccessDenied", TS, "tc1", None, "s1", 1, None),
            (2, "kubectl", {}, "forbidden", TS, "tc2", "11111111-1111-1111-1111-111111111111", "s9", 1, 4),
        ]),
        ("COUNT(DISTINCT", ["category", "calls", "chats"], [("missing_permission", 2, 2)]),
    ])
    out = ca.search_failed_tool_calls(cur, "org-1", {"period": "7d", "issue": "missing_permission",
                                                     "tool": "cloud_exec", "q": "s3"}, limit=20)
    assert [c["link"] for c in out["tool_calls"]] == [
        "/chat?sessionId=s1", "/incidents/11111111-1111-1111-1111-111111111111"]
    sql, params = cur.calls[0]
    assert "missing_permission" in params and "cloud_exec" in params and "%s3%" in params
    assert out["by_issue"][0] == {"category": "missing_permission", "label": "Missing permission",
                                  "calls": 2, "chats": 2}


def test_search_failed_tool_calls_rejects_bad_source():
    with pytest.raises(InvalidFilter):
        ca.search_failed_tool_calls(Cursor([]), "org-1", {"source": "slack"})


def test_titles_are_redacted():
    row = dict(zip(_CHAT_COLS, _chat_row()))
    row["title"] = "debug psql --password hunter2"
    assert "hunter2" not in ca._chat_summary(row)["title"]


def test_issue_badges_and_filters_use_the_selected_period():
    f = ChatFilters.parse({"issue": "any", "period": "30d"})
    sql, params = ca._chat_where(f, "o")
    assert sql.count("es.started_at >= NOW() - %s::interval") == 1 and params.count("30 days") == 2
    cur = Cursor([])
    ca._issue_counts_for(cur, "o", ["s1"], "30d")
    assert "es.started_at >= NOW()" in cur.calls[0][0] and "30 days" in cur.calls[0][1]


def test_snippet_window_is_taken_around_the_match():
    cur = Cursor([])
    ca._snippets_for(cur, "o", ["s1"], '"permission denied"')
    sql, params = cur.calls[0]
    assert "strpos(lower(e.m->>'text'), %s)" in sql
    assert params[:3] == ("permission", ca.SNIPPET_WINDOW // 2, ca.SNIPPET_WINDOW)


# --- role scoping of the agent tool's query --------------------------------

def _scoped_search(owner):
    cur = Cursor([("COUNT(DISTINCT", ["category", "calls", "chats"], [])])
    out = ca.search_failed_tool_calls(cur, "org-1", {"period": "7d"}, owner_user_id=owner)
    return out, cur.calls


def test_org_scope_has_no_owner_restriction():
    out, calls = _scoped_search(None)
    assert out["scope"] == "org"
    assert all("own.user_id" not in sql and "cs.user_id" not in sql for sql, _ in calls)


def test_own_scope_restricts_calls_and_issue_counts():
    out, calls = _scoped_search("viewer-1")
    assert out["scope"] == "own"
    (list_sql, list_params), (totals_sql, totals_params) = calls
    assert "own.user_id = %s" in list_sql and "own.org_id = es.org_id" in list_sql and "viewer-1" in list_params
    assert "cs.user_id = %s" in totals_sql and "viewer-1" in totals_params   # counts can't leak org totals
    for _, params in calls:
        assert "org-1" in params                                            # still org-scoped


def test_owner_cannot_come_from_the_model_arguments():
    cur = Cursor([])
    out = ca.search_failed_tool_calls(
        cur, "org-1", {"owner_user_id": "someone", "scope": "org", "user": "other"}, owner_user_id="viewer-1")
    assert out["scope"] == "own"
    assert all("someone" not in p and "other" not in p for _, p in cur.calls)


def test_all_chats_count_ignores_only_the_issue_filter():
    cur = Cursor([("SELECT COUNT(*) FROM chat_sessions", ["count"], [(4,)])])
    out = ca.list_chats(cur, "org-1", ChatFilters.parse({"issue": "timeout", "source": "slack"}))
    filtered_sql, all_sql = cur.calls[0][0], cur.calls[1][0]
    assert "EXISTS (SELECT 1 FROM execution_steps" in filtered_sql
    assert "EXISTS (SELECT 1 FROM execution_steps" not in all_sql and "= %s" in all_sql  # source kept
    assert out["all_chats"] == 4

    cur = Cursor([("SELECT COUNT(*) FROM chat_sessions", ["count"], [(9,)])])
    assert ca.list_chats(cur, "org-1", ChatFilters.parse({}))["all_chats"] == 9
    assert sum("SELECT COUNT(*) FROM chat_sessions" in sql for sql, _ in cur.calls) == 1
