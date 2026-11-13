"""The agent's read-only conversation-audit tools.

search_tool_calls, search_conversations and get_conversation answer the same
questions as Monitor → Chat history, under the same rule (may_audit_chats),
checked on every call and never widened by arguments.
"""

import json
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

import chat.backend.agent.tools.introspection_tools as tools
from chat.backend.agent.utils.state import State
import services.monitor.chat_audit as audit

REAL_LIST_TOOL_CALLS = audit.list_tool_calls  # before the fixture stubs it

ISSUES = [{"category": "missing_permission", "label": "Missing permission", "calls": 3, "chats": 2}]


@pytest.fixture
def env(monkeypatch):
    cur = MagicMock()
    # Default: an interactive web-app chat (no trigger metadata, not background).
    state = {"allowed": True, "meta": None, "background": False, "incident": None, "calls": {}}

    @contextmanager
    def fake_cursor(user_id):
        yield cur, "org-1"

    monkeypatch.setattr(tools, "_cursor", fake_cursor)
    monkeypatch.setattr("utils.auth.viewer_run.may_audit_chats", lambda u, o: state["allowed"])
    monkeypatch.setattr("utils.auth.connector_access.pinned_roles", lambda: None)
    # The real agent State, as set by set_user_context, not a stand-in object.
    monkeypatch.setattr("utils.cloud.cloud_utils.get_state_context", lambda: State(
        question="q", trigger_metadata=state["meta"], is_background=state["background"],
        incident_id=state["incident"]))

    def recorder(name, result):
        def fn(c, org_id, raw):
            state["calls"][name] = {"cur": c, "org_id": org_id, "raw": raw}
            return result
        return fn

    monkeypatch.setattr(audit, "list_tool_calls", recorder("tool_calls", {
        "tool_calls": [{
            "started_at": "t", "tool_name": "cloud_exec", "input": "aws s3 ls", "status": "failed",
            "category": "missing_permission", "reason": "AccessDenied",
            "user": {"id": "u2", "email": "b@x.io", "name": "B"}, "source": "slack",
            "chat_title": "Deploy check", "link": "/chat?sessionId=s1",
        }],
        "total": 1, "status_counts": {"failed": 1, "succeeded": 0, "running": 0, "total": 1}, "issue_counts": ISSUES,
    }))
    monkeypatch.setattr(audit, "list_chats", lambda c, org_id, f: (
        state["calls"].__setitem__("chats", {"org_id": org_id, "filters": f}) or {
            "chats": [{
                "id": "s1", "title": "Deploy check", "user": {"id": "u2", "email": "b@x.io", "name": "B"},
                "source": "slack", "status": "completed", "created_at": "t", "updated_at": "t",
                "message_count": 4, "issues": {"missing_permission": 1}, "failed_tool_calls": 1,
                "match": {"message_index": 2, "sender": "aurora", "text": "it was AccessDenied"},
                "link": "/chat?sessionId=s1",
            }],
            "total": 1, "issue_counts": ISSUES,
        }))
    long_text = "word " * 1000
    monkeypatch.setattr(audit, "get_transcript", lambda c, org_id, sid, **_kw: (
        state["calls"].__setitem__("transcript", {"org_id": org_id, "sid": sid}) or {
            "chat": {"id": sid, "title": "Deploy check", "user": {"email": "b@x.io"}, "source": "slack",
                     "status": "completed", "created_at": "t", "updated_at": "t", "message_count": 30,
                     "link": f"/chat?sessionId={sid}"},
            "messages": [{"id": i, "sender": "user" if i % 2 else "bot", "text": long_text if i == 29 else f"m{i}",
                          "toolCalls": ([{"tool_name": "cloud_exec", "input": "x" * 900, "status": "error",
                                          "error": "AccessDenied"},
                                         {"tool_name": "kubectl", "input": "{}", "status": "completed", "error": None}]
                                        if i == 29 else [])}
                         for i in range(30)],
            "issues": [{"category": "missing_permission", "tool_name": "cloud_exec", "message_index": 29,
                        "summary": "AccessDenied"}],
        }))
    return state


ALL = [
    ("search_tool_calls", {}),
    ("search_conversations", {}),
    ("get_conversation", {"conversation_id": "s1"}),
]


@pytest.mark.parametrize("name, kwargs", ALL)
def test_refused_when_the_org_rule_says_no(env, name, kwargs):
    env["allowed"] = False
    out = json.loads(getattr(tools, name)(user_id="editor-1", **kwargs))
    assert out == {"error": tools.AUDIT_REFUSAL}
    assert env["calls"] == {}


@pytest.mark.parametrize("name, kwargs", ALL)
def test_slack_guests_are_refused_before_any_query(env, monkeypatch, name, kwargs):
    opened = []
    monkeypatch.setattr("utils.auth.connector_access.pinned_roles", lambda: ["general_viewer"])
    monkeypatch.setattr(tools, "_cursor", lambda uid: opened.append(uid))
    out = json.loads(getattr(tools, name)(user_id="owner-admin", **kwargs))
    assert out == {"error": tools.GUEST_REFUSAL} and opened == []


@pytest.mark.parametrize("name, kwargs", ALL)
def test_arguments_cannot_widen_scope(env, name, kwargs):
    # Extra arguments (as if the model invented them) change nothing: same org,
    # same refusal, and the schemas have no field for another org or scope.
    env["allowed"] = False
    out = json.loads(getattr(tools, name)(user_id="editor-1", org_id="org-2", scope="org", owner_user_id=None,
                                          **kwargs))
    assert out == {"error": tools.AUDIT_REFUSAL}
    for schema in (tools.SearchToolCallsArgs, tools.SearchConversationsArgs, tools.GetConversationArgs):
        assert not {"org_id", "org", "scope", "owner_user_id", "user_id"} & set(schema.model_fields)


def test_search_tool_calls_defaults_to_failures_and_pages(env):
    out = json.loads(tools.search_tool_calls(user_id="admin-1", limit=10, offset=25))
    call = env["calls"]["tool_calls"]
    assert call["org_id"] == "org-1"
    assert call["raw"]["status"] == "failed" and call["raw"]["per_page"] == 10 and call["raw"]["page"] == 3
    assert out["offset"] == 20 and out["total_matching"] == 1
    assert out["tool_calls"][0] == {
        "started_at": "t", "tool": "cloud_exec", "command": "aws s3 ls", "status": "failed",
        "issue": "missing_permission", "reason": "AccessDenied", "user": "b@x.io", "user_id": "u2",
        "source": "slack", "chat_title": "Deploy check", "link": "/chat?sessionId=s1",
    }
    assert out["status_counts"]["failed"] == 1 and out["failures_by_issue"][0]["calls"] == 3


def test_search_tool_calls_passes_every_filter_and_caps_limit(env):
    tools.search_tool_calls(user_id="a", status="all", period="30d", issue="timeout", tool="kubectl",
                            source="slack", user="u9", query="forbidden", limit=500)
    raw = env["calls"]["tool_calls"]["raw"]
    assert raw == {"status": "all", "period": "30d", "issue": "timeout", "tool": "kubectl", "source": "slack",
                   "user": "u9", "q": "forbidden", "page": 1, "per_page": 50}


def test_bad_filters_return_constant_messages(env, monkeypatch):
    # The real service rejects the status before any query.
    monkeypatch.setattr(audit, "list_tool_calls", REAL_LIST_TOOL_CALLS)
    out = json.loads(tools.search_tool_calls(user_id="a", status="weird"))
    assert out == {"error": audit.FILTER_ERRORS["step_status"]}


@pytest.mark.parametrize("meta, background, private", [
    (None, False, True),                                     # web app
    ({"source": "slack", "channel": "D01"}, True, True),     # Slack DM
    ({"source": "slack", "channel": "C01"}, True, False),    # Slack channel
    ({"source": "teams"}, True, False),
    ({"source": "chat", "incident_id": "i1"}, True, False),  # RCA started from chat
    (None, True, False),                                     # background with no metadata
])
def test_search_conversations_snippets_only_in_private_chats(env, meta, background, private):
    env["meta"], env["background"] = meta, background
    out = json.loads(tools.search_conversations(user_id="a", query="AccessDenied", source="slack"))
    match = out["conversations"][0]["match"]
    if private:
        assert match == {"sender": "aurora", "text": "it was AccessDenied"} and "note" not in out
    else:
        assert match == {"matched": True} and out["note"] == tools.SHARED_CHANNEL_NOTE
    assert out["conversations"][0]["link"] == ("/chat?sessionId=s1" if private else tools.CHAT_HISTORY_LINK)
    f = env["calls"]["chats"]["filters"]
    assert env["calls"]["chats"]["org_id"] == "org-1" and f.query == "AccessDenied" and f.source == "slack"


def test_get_conversation_caps_messages_fields_and_lists_failures(env):
    out = json.loads(tools.get_conversation(user_id="a", conversation_id="s1"))
    assert env["calls"]["transcript"] == {"org_id": "org-1", "sid": "s1"}
    assert len(out["messages"]) == tools.TRANSCRIPT_MESSAGES and out["messages_shown"].startswith("last 20 of 30")
    last = out["messages"][-1]
    assert len(last["text"]) <= tools.TRANSCRIPT_TEXT_CHARS + 20
    (failed,) = last["failed_tool_calls"]            # only the failed call, not the successful one
    assert failed["tool"] == "cloud_exec" and failed["reason"] == "AccessDenied"
    assert failed["input"].startswith("x" * tools.TRANSCRIPT_INPUT_CHARS)
    assert len(failed["input"]) <= tools.TRANSCRIPT_INPUT_CHARS + 20
    assert out["issues"][0] == {"issue": "missing_permission", "tool": "cloud_exec", "message_index": 29,
                                "reason": "AccessDenied"}
    assert out["conversation"]["link"] == "/chat?sessionId=s1"


def test_get_conversation_in_a_shared_channel_returns_metadata_only(env):
    env["meta"], env["background"] = {"source": "slack", "channel": "C0123"}, True
    out = json.loads(tools.get_conversation(user_id="a", conversation_id="s1"))
    assert "messages" not in out and out["note"] == tools.SHARED_CHANNEL_NOTE
    assert out["issues"] == [{"issue": "missing_permission", "tool": "cloud_exec", "message_index": 29}]
    assert out["conversation"]["link"] == tools.CHAT_HISTORY_LINK


def test_unknown_context_is_treated_as_shared(env, monkeypatch):
    monkeypatch.setattr("utils.cloud.cloud_utils.get_state_context", lambda: None)
    out = json.loads(tools.get_conversation(user_id="a", conversation_id="s1"))
    assert "messages" not in out


def test_requires_user():
    assert "error" in json.loads(tools.search_conversations())



# --- finding exact text in a conversation -----------------------------------

PHRASE = "Nothing was validated because no root cause was confirmed"


@pytest.fixture
def tail_transcript(env, monkeypatch):
    # The phrase sits past the 1,500-character excerpt of message 4, next to a secret.
    text = ("Classification: INCONCLUSIVE. " + "x" * 2500 + " " + PHRASE
            + ". Then psql --password hunter2 was run.")
    monkeypatch.setattr(audit, "get_transcript", lambda c, org_id, sid, **_kw: {
        "chat": {"id": sid, "title": "RCA", "user": {"email": "b@x.io"}, "source": "rca", "status": "completed",
                 "created_at": "t", "updated_at": "t", "message_count": 6, "link": f"/chat?sessionId={sid}"},
        "messages": [{"id": i, "sender": "bot", "toolCalls": [],
                      "text": audit.scrub(text) if i == 4 else f"m{i} " + ("root cause " if i == 2 else "")}
                     for i in range(6)],
        "issues": [],
    })
    return text


def test_query_finds_a_sentence_past_the_excerpt_limit(tail_transcript):
    assert tail_transcript.index(PHRASE) > tools.TRANSCRIPT_TEXT_CHARS
    out = json.loads(tools.get_conversation(user_id="a", conversation_id="s1", query=PHRASE))
    (hit,) = out["query"]["occurrences"]
    assert hit["message_index"] == 4 and PHRASE in hit["text"]
    assert "hunter2" not in hit["text"]                      # redaction holds inside the window
    assert len(hit["text"]) <= len(PHRASE) + 2 * tools.QUERY_WINDOW_RADIUS + 2


def test_query_tolerates_case_punctuation_and_line_breaks(tail_transcript):
    out = json.loads(tools.get_conversation(user_id="a", conversation_id="s1", query='"nothing was\nvalidated"'))
    assert out["query"]["found"] == 1


def test_query_lists_every_message_that_contains_it(tail_transcript):
    out = json.loads(tools.get_conversation(user_id="a", conversation_id="s1", query="root cause"))
    assert [h["message_index"] for h in out["query"]["occurrences"]] == [2, 4]


def test_message_index_reads_one_message_in_full(tail_transcript):
    out = json.loads(tools.get_conversation(user_id="a", conversation_id="s1", message_index=4))
    assert PHRASE in out["message"]["text"] and "hunter2" not in out["message"]["text"]
    missing = json.loads(tools.get_conversation(user_id="a", conversation_id="s1", message_index=99))
    assert "error" in missing


@pytest.mark.parametrize("kwargs", [{"query": PHRASE}, {"message_index": 4}])
def test_shared_channels_still_get_metadata_only(env, tail_transcript, kwargs):
    env["meta"], env["background"] = {"source": "slack", "channel": "C0123"}, True
    out = json.loads(tools.get_conversation(user_id="a", conversation_id="s1", **kwargs))
    assert out["note"] == tools.SHARED_CHANNEL_NOTE
    assert "query" not in out and "message" not in out and "messages" not in out


def test_search_conversations_reports_match_type(env, monkeypatch):
    monkeypatch.setattr(audit, "list_chats", lambda c, org_id, f: {
        "chats": [{"id": "s1", "title": "t", "user": {}, "source": "rca", "status": "completed",
                   "created_at": "t", "updated_at": "t", "message_count": 6, "issues": {}, "failed_tool_calls": 0,
                   "match": {"message_index": 4, "sender": "aurora", "text": PHRASE, "match_type": "exact"},
                   "match_type": "exact", "link": "/chat?sessionId=s1"}],
        "total": 1, "issue_counts": [],
    })
    out = json.loads(tools.search_conversations(user_id="a", query=PHRASE))
    assert out["conversations"][0]["match_type"] == "exact"


# --- shared channels: every tool trims other people's words -----------------

def test_search_tool_calls_in_a_shared_channel_drops_commands_reasons_and_titles(env):
    env["meta"], env["background"] = {"source": "slack", "channel": "C0123"}, True
    out = json.loads(tools.search_tool_calls(user_id="admin-1"))
    (call,) = out["tool_calls"]
    assert not {"command", "reason", "chat_title"} & set(call)
    assert call["tool"] == "cloud_exec" and call["issue"] == "missing_permission"
    # A chat link would open the raw chat for any org member: point at the audit page instead.
    assert call["link"] == tools.CHAT_HISTORY_LINK
    assert out["note"] == tools.SHARED_CHANNEL_NOTE


def test_search_conversations_in_a_shared_channel_drops_titles(env):
    env["meta"], env["background"] = {"source": "teams"}, True
    out = json.loads(tools.search_conversations(user_id="a"))
    assert "title" not in out["conversations"][0]
    assert out["conversations"][0]["link"] == tools.CHAT_HISTORY_LINK


def test_shared_answers_keep_incident_links_and_private_ones_keep_chat_links():
    assert tools._link("/incidents/inc-1", False) == "/incidents/inc-1"
    assert tools._link("/chat?sessionId=s1", True) == "/chat?sessionId=s1"
    assert tools._link(None, False) is None


def test_a_follow_up_in_an_incident_chat_gets_the_shared_answer(env):
    # Same web chat, but on an incident: its tool output lands in the timeline.
    env["incident"] = "inc-1"
    out = json.loads(tools.search_conversations(user_id="a"))
    assert out["note"] == tools.SHARED_CHANNEL_NOTE and "title" not in out["conversations"][0]


def test_lower_bound_totals_are_reported(env, monkeypatch):
    monkeypatch.setattr(audit, "list_tool_calls", lambda c, o, r: {
        "tool_calls": [], "total": 500, "search_capped": True,
        "status_counts": {"failed": 0, "succeeded": 0, "running": 0, "total": 0}, "issue_counts": []})
    assert json.loads(tools.search_tool_calls(user_id="a", query="x"))["total_is_lower_bound"] is True


def test_get_conversation_loads_the_latest_messages(env, monkeypatch):
    seen = {}
    monkeypatch.setattr(audit, "get_transcript", lambda c, o, sid, **kw: seen.update(kw) or None)
    tools.get_conversation(user_id="a", conversation_id="s1")
    assert seen == {"tail": True}


def test_background_chats_carry_their_trigger_metadata_into_state():
    # The rule is only as good as the metadata it reads: the background task
    # must put it on the State the tools see.
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "chat" / "background" / "task.py").read_text()
    assert "trigger_metadata=dict(trigger_metadata) if trigger_metadata else None" in source
    state = State(question="q", is_background=True,
                  trigger_metadata={"source": "slack", "channel": "C0123"})
    assert audit.private_answer_channel(state.trigger_metadata, is_background=state.is_background) is False
    assert "trigger_metadata" in State.model_fields
