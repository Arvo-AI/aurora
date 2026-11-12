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
    from utils.db.chat_audit_indexes import INDEXES
    fts = dict((name, d) for name, _, d in INDEXES)["idx_chat_sessions_message_fts"]
    assert ca.SEARCH_DOC_SQL.replace("cs.messages", "messages") in fts


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


def test_list_shapes_rows_badges_and_facets():
    cur = Cursor([
        ("SELECT COUNT(*) FROM chat_sessions", ["count"], [(1,)]),
        ("ORDER BY cs.updated_at DESC", _CHAT_COLS, [_chat_row()]),
        ("GROUP BY 1, 2", ["sid", "category", "count"], [("s1", "missing_permission", 2), ("s1", "other", 1)]),
        ("COUNT(DISTINCT", ["category", "calls", "chats"], [("missing_permission", 12, 3)]),
        ("GROUP BY cs.user_id", ["uid", "email", "name", "n"], [("u1", "a@x.io", "A", 5)]),
        ("FROM chat_sessions cs WHERE", ["source", "count"], [("slack", 4)]),
    ])
    out = ca.list_chats(cur, "org-1", ChatFilters.parse({"issue": "any"}))

    chat = out["chats"][0]
    assert chat["source"] == "slack" and chat["user"]["email"] == "a@x.io"
    assert chat["issues"] == {"missing_permission": 2} and chat["failed_tool_calls"] == 3
    assert "failures" not in chat                     # failures have their own view now

    totals = {c["category"]: c for c in out["issue_counts"]}
    assert totals["missing_permission"]["calls"] == 12 and totals["blocked_command"]["calls"] == 0
    assert {s["source"]: s["count"] for s in out["sources"]}["slack"] == 4
    for sql, params in cur.calls:  # every query is scoped to the caller's org
        assert "org-1" in params, sql


def test_transcript_failures_compute_anchors_once_per_chat():
    cur = Cursor([])
    ca._failed_tool_calls(cur, "org-1", ["a"], 10)
    sql, params = cur.calls[0]
    assert sql.count("jsonb_array_elements(") == 2 and "c.id IN (SELECT DISTINCT chat_id FROM f)" in sql
    assert params == ("org-1", ["a"], 10, "org-1")


def test_deleted_chats_are_excluded_but_rca_rows_kept():
    cur = Cursor([])
    ca._failed_tool_calls(cur, "org-1", ["a"], 10)
    ca._issue_totals(cur, "org-1", "7d")
    calls = list(cur.calls)
    page = _ftc_cursor([])
    ca.list_tool_calls(page, "org-1", {})
    for sql, _ in calls + page.calls:
        if "execution_steps es" in sql:
            assert ca._NOT_DELETED_SQL in sql, sql
    assert "es.incident_id IS NOT NULL OR" in ca._NOT_DELETED_SQL and "act.is_active" in ca._NOT_DELETED_SQL


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
        ("WITH f AS", _FAIL_COLS, list(failures)),
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


# --- redaction of real-world shapes (review findings) -----------------------

STS_OUTPUT = json.dumps({"Credentials": {
    "AccessKeyId": "ASIAEXAMPLEID", "SecretAccessKey": "wJalrXUtnFEMIexamplesecretvalue",
    "SessionToken": "FwoGZXIvYXdzEXAMPLEsessiontokenvalue", "Expiration": "2026-11-12T10:00:00Z"}})
K8S_SECRET = json.dumps({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "db"},
                         "data": {"DATABASE_URL": "cG9zdGdyZXM6Ly91OnBAaA==", "tls.key": "LS0tLS1CRUdJTg=="}})


def test_sts_credentials_are_redacted_by_key_name():
    # The whole "Credentials" object goes; its fields are caught on their own too.
    assert json.loads(ca.scrub_json_text(STS_OUTPUT))["Credentials"] == ca.REDACTED
    inner = json.loads(ca.scrub_json_text(json.dumps(json.loads(STS_OUTPUT)["Credentials"])))
    assert inner["SecretAccessKey"] == inner["SessionToken"] == ca.REDACTED
    assert inner["Expiration"] == "2026-11-12T10:00:00Z"


def test_kubernetes_secret_data_is_redacted_whatever_the_key():
    out = json.loads(ca.scrub_json_text(K8S_SECRET))
    assert out["data"] == ca.REDACTED and out["metadata"] == {"name": "db"}


@pytest.mark.parametrize("key, secret", [
    ("DB_PASS", True), ("tls.key", True), ("client_secret", True), ("privateKey", True),
    ("Authorization", True), ("keyboard_layout", False), ("PrivateIpAddress", False),
    ("author", False), ("authorName", False), ("KeyName", False),
])
def test_secret_key_names(key, secret):
    out = json.loads(ca.scrub_json_text(json.dumps({key: "value123"})))
    assert (out[key] == ca.REDACTED) is secret


def test_snippet_never_shows_a_secret_whose_flag_falls_outside_the_window():
    text = "run --password " + "s3cretvalue" + " x" * (ca.SNIPPET_RADIUS // 2) + " needle " + "y" * 50
    assert "s3cretvalue" not in ca._snippet(text, "needle")


def test_failure_reason_and_transcript_redact_before_truncating():
    long_error = "x" * (ca.REASON_MAX_CHARS - 12) + " --password hunter2longer"
    assert "hunter2" not in ca.failure_reason(long_error)
    secret_key = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    assert secret_key[:12] not in ca.scrub_json_text("token: " + secret_key, limit=14)
    assert "hunter2" not in ca.summarize_input({"command": "psql --password hunter2"})


# --- readable reasons from JSON tool errors -------------------------------

KUBECTL_STDERR = (
    'E1112 10:01:02.123456   4242 memcache.go:265] couldn\'t get current server API group list: '
    'Get "https://10.0.0.5:6443/api?timeout=32s": dial tcp 10.0.0.5:6443: connect: connection refused\n'
    "The connection to the server 10.0.0.5:6443 was refused - did you specify the right host or port?"
)


def test_reason_from_a_real_terminal_exec_error():
    # Exactly what terminal_exec returns for a failed command (json.dumps(..., indent=2)).
    payload = json.dumps({
        "success": False, "command": "kubectl get pods -A", "final_command": "kubectl get pods -A",
        "return_code": 1, "chat_output": KUBECTL_STDERR, "working_dir": "/home/aurora", "provider": "terminal",
    }, indent=2)
    reason = ca.failure_reason(payload)
    assert reason != "{" and "connection refused" in reason
    assert reason.startswith("E1112")


def test_reason_from_a_multi_subscription_cloud_exec_error():
    payload = json.dumps({
        "success": False, "multi_subscription": True, "command": "az vm list", "provider": "azure",
        "results_by_subscription": {
            "sub-ok": {"subscription_id": "sub-ok", "success": True, "output": "[]", "return_code": 0},
            "sub-bad": {"subscription_id": "sub-bad", "success": False, "return_code": 1,
                        "output": "(AuthorizationFailed) The client does not have authorization to perform action"},
        },
    })
    assert ca.failure_reason(payload) == (
        "sub-bad: (AuthorizationFailed) The client does not have authorization to perform action")


def test_reason_falls_back_to_redacted_compact_json_never_a_brace():
    reason = ca.failure_reason(json.dumps({"success": False, "api_key": "abc", "code": 7}, indent=2))
    assert reason not in ("{", "") and "abc" not in reason



# --- tool calls view ----------------------------------------------------------

_FTC_COLS = ["id", "tool_name", "tool_input", "status", "error_message", "started_at", "tool_call_id",
             "incident_id", "chat_id", "chat_title", "user_id", "source", "message_index",
             "user_email", "user_name"]


def _ftc_row(i, command, error, *, title="Deploy check", user="u1", source="slack", msg=2, status="error"):
    return (i, "cloud_exec", {"provider": "aws", "command": command}, status, error, TS, f"tc{i}",
            None, "s1", title, user, source, msg, "a@x.io", "A")


def _ftc_cursor(rows, total=None):
    return Cursor([
        ("SELECT COUNT(*) FROM execution_steps", ["count"], [(len(rows) if total is None else total,)]),
        ("WITH f AS", _FTC_COLS, rows),
        ("COUNT(DISTINCT", ["category", "calls", "chats"], [("missing_permission", 19, 4)]),
        ("SELECT es.status, COUNT(*)", ["status", "count"], [("error", 19), ("success", 40), ("running", 1)]),
        ("es.tool_name AS value", ["value", "count"], [("cloud_exec", 19)]),
        ("AS value, COUNT(*), MAX(u.email)", ["value", "count", "email", "name"], [("u1", 19, "a@x.io", "A")]),
        ("AS value, COUNT(*)", ["value", "count"], [("slack", 19)]),
    ])


def test_failed_tool_calls_page_is_shaped_redacted_and_org_scoped():
    cur = _ftc_cursor([_ftc_row(1, "ec2 describe-instances --password hunter2",
                                '{"error": "AccessDenied: not authorized"}',
                                title="psql --password hunter2")], total=19)
    out = ca.list_tool_calls(cur, "org-1", {"period": "7d", "per_page": "10", "page": "2"})
    row = out["tool_calls"][0]
    assert row["tool_name"] == "cloud_exec" and row["category"] == "missing_permission"
    assert row["reason"] == "AccessDenied: not authorized" and row["message_index"] == 2
    assert "hunter2" not in row["input"] and "hunter2" not in row["chat_title"]
    assert row["source"] == "slack" and row["user"]["email"] == "a@x.io"
    assert (out["total"], out["page"], out["total_pages"]) == (19, 2, 2)
    assert out["issue_counts"][0]["calls"] == 19
    assert out["status_counts"] == {"failed": 19, "succeeded": 40, "running": 1, "total": 60}
    assert row["status"] == "failed"
    assert out["tools"] == [{"value": "cloud_exec", "count": 19}]
    page_sql, page_params = next(c for c in cur.calls if "WITH f AS" in c[0])
    assert ca._NOT_DELETED_SQL in page_sql and page_params[-3:] == (10, 10, "org-1")
    for sql, params in cur.calls:
        assert "org-1" in params, sql


def test_failed_tool_calls_filters_bind_source_user_tool_and_issue():
    cur = _ftc_cursor([])
    ca.list_tool_calls(cur, "org-1", {"source": "slack", "user": "u9", "tool": "kubectl",
                                             "issue": "blocked_command"})
    count_sql, count_params = cur.calls[0]
    assert ca.SOURCE_SQL in count_sql and "cs.user_id = %s" in count_sql and "es.tool_name = %s" in count_sql
    assert {"slack", "u9", "kubectl", "blocked_command"} <= set(count_params)
    # A facet drops only its own filter, so the dropdown still lists the alternatives.
    tool_sql, tool_params = next(c for c in cur.calls if "es.tool_name AS value" in c[0])
    assert "kubectl" not in tool_params and "slack" in tool_params and "u9" in tool_params


def test_failed_tool_calls_search_matches_only_redacted_text():
    rows = [_ftc_row(1, "psql --password hunter2", "login failed"),
            _ftc_row(2, "kubectl get pods", "Error from server (Forbidden): pods is forbidden")]
    cur = _ftc_cursor(rows)
    assert ca.list_tool_calls(cur, "org-1", {"q": "hunt"})["tool_calls"] == []
    out = ca.list_tool_calls(_ftc_cursor(rows), "org-1", {"q": "forbidden"})
    assert [r["tool_call_id"] for r in out["tool_calls"]] == ["tc2"] and out["total"] == 1
    assert not any("ILIKE" in sql for sql, _ in cur.calls)
    assert not any(sql.startswith("SELECT COUNT(*) FROM execution_steps") for sql, _ in cur.calls)


def test_failed_tool_calls_rejects_bad_filters():
    with pytest.raises(InvalidFilter):
        ca.list_tool_calls(Cursor([]), "org-1", {"issue": "bogus"})



def test_tool_calls_default_to_every_status():
    cur = _ftc_cursor([])
    ca.list_tool_calls(cur, "org-1", {})
    count_sql, _ = cur.calls[0]
    assert "es.status = %s" not in count_sql and "es.status = 'error'" not in count_sql


@pytest.mark.parametrize("raw, stored", [
    ({"status": "succeeded"}, "success"), ({"status": "running"}, "running"), ({"status": "failed"}, "error"),
    ({"issue": "timeout"}, "error"), ({"issue": "any"}, "error"),
    ({"status": "succeeded", "issue": "timeout"}, "error"),   # an issue implies failed
])
def test_status_filter_binding(raw, stored):
    cur = _ftc_cursor([])
    ca.list_tool_calls(cur, "org-1", raw)
    count_sql, count_params = cur.calls[0]
    assert "es.status = %s" in count_sql and stored in count_params


def test_bad_status_is_a_constant_error_and_chat_status_values_are_rejected():
    for bad in ("weird", "completed", "<b>x</b>"):
        with pytest.raises(InvalidFilter) as exc:
            ca.list_tool_calls(Cursor([]), "org-1", {"status": bad})
        assert ca.filter_error_message(exc.value) == ca.FILTER_ERRORS["step_status"]


def test_succeeded_rows_have_no_issue_or_reason_and_never_show_output():
    row = _ftc_row(5, "kubectl get pods", None, status="success")
    out = ca.list_tool_calls(_ftc_cursor([row]), "org-1", {})
    shaped = out["tool_calls"][0]
    assert shaped["status"] == "succeeded" and shaped["category"] is None and shaped["reason"] == ""
    assert "output" not in shaped and "error" not in shaped
    assert shaped["input"] == "kubectl get pods"


def test_search_matches_succeeded_rows_by_tool_and_command():
    rows = [_ftc_row(1, "s3 ls", None, status="success"), _ftc_row(2, "ec2 describe-vpcs", "AccessDenied")]
    out = ca.list_tool_calls(_ftc_cursor(rows), "org-1", {"q": "s3 ls"})
    assert [r["tool_call_id"] for r in out["tool_calls"]] == ["tc1"]



# --- links and the shared-channel rule ---------------------------------------

def test_links_point_to_the_incident_or_the_chat():
    assert ca.link_for("s1", None) == "/chat?sessionId=s1"
    assert ca.link_for("s1", "inc-1") == "/incidents/inc-1"
    assert ca.link_for(None, None) is None


@pytest.mark.parametrize("meta, private", [
    (None, True), ({}, True), ({"source": "chat"}, True),
    ({"source": "slack", "channel": "D0123"}, True),          # Slack DM
    ({"source": "slack", "channel": "C0123"}, False),         # Slack channel
    ({"source": "slack"}, False),
    ({"source": "teams", "channel_id": "x"}, False),
    ({"source": "google_chat", "space_name": "spaces/x"}, False),
    ({"source": "action"}, False), ({"source": "pagerduty"}, False),
    ("not-a-dict", False),                                   # malformed: fail closed
])
def test_private_answer_channel(meta, private):
    assert ca.private_answer_channel(meta) is private
