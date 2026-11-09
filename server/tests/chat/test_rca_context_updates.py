"""Regression cases for live RCA context delivery; no model API calls.

Known failures are strict xfails until the corresponding repair stage. Run
with --runxfail to see the original failures. The queue race test additionally
requires RCA_TEST_REDIS_URL and touches only its own random test key.
"""

import asyncio
import importlib.util
import json
import sys
import uuid
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest


SERVER = Path(__file__).resolve().parents[2]


def _load_module(monkeypatch, name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def context_updates(monkeypatch):
    # Avoid chat.background.__init__ launching imports of Celery, routes and
    # summarization. Execute the complete production module, not copied bodies.
    background = ModuleType("chat.background")
    background.__path__ = [str(SERVER / "chat/background")]
    task = ModuleType("chat.background.task")
    task.TERMINAL_SESSION_STATUSES = frozenset({"completed", "failed", "cancelled"})
    monkeypatch.setitem(sys.modules, "chat.background", background)
    monkeypatch.setitem(sys.modules, "chat.background.task", task)

    # These dependencies are not under test; prohibit database access.
    for name, attrs in {
        "utils.cache.redis_client": {"get_redis_client": lambda: None},
        "utils.db.connection_pool": {"db_pool": None},
        "utils.auth.stateless_auth": {"set_rls_context": None},
    }.items():
        module = ModuleType(name)
        module.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, module)

    module = _load_module(
        monkeypatch,
        "chat.background.context_updates",
        SERVER / "chat/background/context_updates.py",
    )
    monkeypatch.setattr(module, "_get_session_status", lambda *_: "in_progress")
    monkeypatch.setattr(module, "get_workflow_context", lambda: None)
    return module


@pytest.mark.parametrize("source,payload,expected", [
    ("grafana", {"title": "Checkout errors", "message": "HTTP 503"}, "HTTP 503"),
    ("pagerduty", {"event": {"data": {
        "title": "Database errors", "body": {"details": "Connection refused"},
        "service": {"summary": "orders"}, "urgency": "high",
    }}}, "Connection refused"),
    ("jenkins", {"application": "checkout", "pipeline_name": "deploy",
                 "body": "Commit abc123 failed"}, "Commit abc123 failed"),
    ("manual_merge", {"title": "Merged investigation", "merged_from_incident": "other",
                      "body": "## Summary\nDatabase saturation"}, "Database saturation"),
])
def test_single_update_keeps_existing_payload_formats(context_updates, source, payload, expected):
    rendered = context_updates._format_updates_for_prompt([{"source": source, "payload": payload}])
    assert expected in rendered


def test_batch_keeps_each_alert_and_its_fields(context_updates):
    updates = [
        {"source": "grafana", "payload": {
            "title": "ALERT-FIRST", "message": "DETAIL-FIRST",
            "service": "SERVICE-FIRST", "severity": "high",
        }},
        {"source": "jenkins", "payload": {
            "title": "ALERT-SECOND", "body": "DETAIL-SECOND",
            "service": "SERVICE-SECOND", "severity": "critical",
        }},
    ]
    rendered = context_updates._format_updates_for_prompt(updates)
    missing = [value for value in (
        "ALERT-FIRST", "DETAIL-FIRST", "SERVICE-FIRST", "grafana",
        "ALERT-SECOND", "DETAIL-SECOND", "SERVICE-SECOND", "jenkins",
    ) if value.lower() not in rendered.lower()]
    assert not missing, f"Consumed alert fields missing from the model message: {missing}"
    assert rendered.index("DETAIL-FIRST") < rendered.index("ALERT-SECOND")


def test_missing_fields_are_not_borrowed_from_a_later_alert(context_updates):
    rendered = context_updates._format_updates_for_prompt([
        {"source": "grafana", "payload": {"title": "TITLE-ONLY"}},
        {"source": "jenkins", "payload": {
            "title": "DEPLOYMENT", "body": "BUILD-FAILED", "service": "checkout",
        }},
    ])
    assert "DEPLOYMENT" in rendered
    first, second = rendered.split("DEPLOYMENT", 1)
    assert "TITLE-ONLY" in first
    assert "BUILD-FAILED" not in first
    assert "**Service:** checkout" not in first
    assert "BUILD-FAILED" in second
    assert "**Service:** checkout" in second


def test_manual_merge_formatting_is_local_to_its_update(context_updates):
    merged_body = "## Summary\nMERGED-EVIDENCE\n## Investigation progress\nQuery complete"
    rendered = context_updates._format_updates_for_prompt([
        {"source": "grafana", "payload": {
            "title": "NORMAL-ALERT", "body": "Summary of an ordinary alert",
        }},
        {"source": "manual_merge", "payload": {
            "title": "MERGED-ALERT", "merged_from_incident": "other", "body": merged_body,
        }},
    ])
    assert "MERGED-ALERT" in rendered
    ordinary, merged = rendered.split("MERGED-ALERT", 1)
    assert "**Details:** Summary of an ordinary alert" in ordinary
    assert "**Context from merged incident:**" not in ordinary
    assert "**Context from merged incident:**" in merged
    assert merged_body in merged


def test_empty_queue_returns_no_updates(context_updates, monkeypatch):
    client = MagicMock()
    client.lrange.return_value = []
    monkeypatch.setattr(context_updates, "get_redis_client", lambda: client)
    assert context_updates.drain_rca_context_updates("user", "session") == []


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="Bug 2: a producer write between LRANGE and DELETE is lost")
def test_update_arriving_during_drain_is_not_lost(context_updates, monkeypatch):
    import os

    url = os.getenv("RCA_TEST_REDIS_URL")
    if not url:
        pytest.skip("Set RCA_TEST_REDIS_URL to an isolated/local test Redis")
    from redis import Redis

    consumer = Redis.from_url(url, decode_responses=True)
    producer = Redis.from_url(url, decode_responses=True)
    user_id = "rca-regression-" + uuid.uuid4().hex
    session_id = str(uuid.uuid4())
    key = context_updates._make_update_key(user_id, session_id)
    first = {"source": "grafana", "payload": {"message": "BEFORE-DRAIN"}}
    arriving = {"source": "jenkins", "payload": {"message": "DURING-DRAIN"}}
    inserted = False

    def insert_after_response():
        nonlocal inserted
        if not inserted:
            inserted = True
            producer.rpush(key, json.dumps(arriving))

    # Schedule the producer after the consumer's first network round trip.
    # Instrument pipelines too, so the test does not require a particular fix.
    execute = consumer.execute_command
    pipeline = consumer.pipeline

    def interleave_command(*args, **kwargs):
        result = execute(*args, **kwargs)
        insert_after_response()
        return result

    def interleave_pipeline(*args, **kwargs):
        batch = pipeline(*args, **kwargs)
        run = batch.execute

        def execute_batch(*a, **kw):
            result = run(*a, **kw)
            insert_after_response()
            return result

        batch.execute = execute_batch
        return batch

    try:
        producer.rpush(key, json.dumps(first))
        producer.expire(key, 60)
        monkeypatch.setattr(consumer, "execute_command", interleave_command)
        monkeypatch.setattr(consumer, "pipeline", interleave_pipeline)
        monkeypatch.setattr(context_updates, "get_redis_client", lambda: consumer)
        drained = context_updates.drain_rca_context_updates(user_id, session_id)
        remaining = [json.loads(raw) for raw in producer.lrange(key, 0, -1)]
        assert inserted, "The controlled producer must actually run"
        assert drained == [first]
        assert remaining == [arriving], "An accepted update disappeared without being consumed"
    finally:
        producer.delete(key)
        consumer.close()
        producer.close()


def test_foreground_chat_does_not_consume_rca_updates(context_updates, monkeypatch):
    drain = MagicMock()
    monkeypatch.setattr(context_updates, "drain_rca_context_updates", drain)
    assert context_updates.apply_rca_context_updates(SimpleNamespace(is_background=False)) is None
    drain.assert_not_called()


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="Bug 3: request override does not persist a context message in the agent graph")
def test_context_survives_later_agent_model_turns(context_updates, monkeypatch):
    for package in ("langchain", "langgraph", "langchain-core"):
        try:
            version(package)
        except PackageNotFoundError:
            pytest.skip(f"{package} is required for the real graph regression")

    from langchain.agents import create_agent
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.tools import tool
    from chat.backend.agent.utils.state import State
    from utils.cloud import cloud_utils

    # Only model-size lookup is replaced; graph and middleware behavior are real.
    manager = ModuleType("chat.backend.agent.utils.chat_context_manager")
    manager.ChatContextManager = SimpleNamespace(get_context_limit=lambda _: 128000)
    monkeypatch.setitem(sys.modules, manager.__name__, manager)
    middleware = _load_module(
        monkeypatch,
        "chat.backend.agent.middleware.context_trim",
        SERVER / "chat/backend/agent/middleware/context_trim.py",
    )
    state = State(question="Investigate synthetic order failures", user_id="test-user",
                  session_id="test-session", is_background=True, rca_context={"source": "grafana"})
    token = cloud_utils._state_var.set(state)
    pending = []
    seen = []
    note = "UNIQUE-CONTEXT: only the Canada tenant is affected"
    update = {"source": "jenkins", "payload": {"title": "Deployment context", "body": note}}
    monkeypatch.setattr(context_updates, "drain_rca_context_updates", lambda *_: [pending.pop(0)] if pending else [])

    class ScriptedModel(BaseChatModel):
        @property
        def _llm_type(self):
            return "rca-regression-scripted"

        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            seen.append(list(messages))
            tool_calls = [] if len(seen) == 3 else [{
                "name": "read_evidence", "args": {"step": len(seen)},
                "id": f"read-{len(seen)}", "type": "tool_call",
            }]
            message = AIMessage(content="Done" if not tool_calls else "Read evidence", tool_calls=tool_calls)
            return ChatResult(generations=[ChatGeneration(message=message)])

    @tool
    def read_evidence(step: int) -> str:
        """Read synthetic evidence without querying a real system."""
        if step == 1:
            pending.append(update)
        return f"SYNTHETIC-EVIDENCE-{step}"

    async def run():
        agent = create_agent(model=ScriptedModel(), tools=[read_evidence],
                             middleware=[middleware.ContextSafetyMiddleware("test-model")])
        result = None
        # Match agentic_tool_flow's astream_events path, not only ainvoke.
        async for event in agent.astream_events(
            {"messages": [HumanMessage(content=state.question)]},
            config={"recursion_limit": 10}, version="v2",
        ):
            if event["event"] == "on_chain_end" and event["name"] == "LangGraph":
                result = event["data"]["output"]
        return result

    try:
        output = asyncio.run(run())
    finally:
        cloud_utils._state_var.reset(token)

    assert len(seen) == 3
    def contains_note(messages):
        return any(isinstance(m, HumanMessage) and note in str(m.content) for m in messages)
    assert not contains_note(seen[0])
    assert contains_note(seen[1]), "The update must reach its first eligible request"
    assert state.rca_ui_updates, "The production helper must create its UI record"
    assert any(isinstance(m, ToolMessage) and m.content == "SYNTHETIC-EVIDENCE-1" for m in seen[2])
    assert contains_note(seen[2]), "The original context vanished on the following model turn"
    assert contains_note(output["messages"])
