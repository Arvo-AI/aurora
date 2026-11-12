"""Regression cases for live RCA context delivery; no model API calls by default.

Real-Redis cases require RCA_TEST_REDIS_URL and touch only random test keys.
Graph cases use installed LangChain/LangGraph with a scripted model.
The opt-in live variant requires RCA_TEST_LIVE_MODEL=1 and configured model auth.
"""

import asyncio
import importlib.util
import json
import os
import re
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
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
    transaction = client.pipeline.return_value.__enter__.return_value
    transaction.execute.return_value = [[], 0]
    monkeypatch.setattr(context_updates, "get_redis_client", lambda: client)
    assert context_updates.drain_rca_context_updates("user", "session") == []
    transaction.execute.assert_called_once()


def test_update_arriving_during_drain_is_not_lost(context_updates, monkeypatch):
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


@pytest.fixture
def redis_queue(context_updates, monkeypatch):
    url = os.getenv("RCA_TEST_REDIS_URL")
    if not url:
        pytest.skip("Set RCA_TEST_REDIS_URL to an isolated/local test Redis")
    from redis import Redis

    client = Redis.from_url(url, decode_responses=True)
    user_id = "rca-regression-" + uuid.uuid4().hex
    session_id = str(uuid.uuid4())
    key = context_updates._make_update_key(user_id, session_id)
    monkeypatch.setattr(context_updates, "get_redis_client", lambda: client)
    try:
        yield SimpleNamespace(client=client, user_id=user_id, session_id=session_id, key=key)
    finally:
        client.delete(key)
        client.close()


@pytest.mark.parametrize("count", [1, 3])
def test_queue_drains_in_order_and_only_once(context_updates, redis_queue, count):
    queue = redis_queue
    assert context_updates.drain_rca_context_updates(queue.user_id, queue.session_id) == []
    updates = [{"source": "grafana", "payload": {"message": f"alert-{i}"}} for i in range(count)]
    queue.client.rpush(queue.key, *(json.dumps(update) for update in updates))
    queue.client.expire(queue.key, 60)

    assert context_updates.drain_rca_context_updates(queue.user_id, queue.session_id) == updates
    assert context_updates.drain_rca_context_updates(queue.user_id, queue.session_id) == []
    assert not queue.client.exists(queue.key)


def test_drain_does_not_touch_another_session(context_updates, redis_queue):
    queue = redis_queue
    other_session = str(uuid.uuid4())
    other_key = context_updates._make_update_key(queue.user_id, other_session)
    first = {"payload": {"message": "FIRST-SESSION"}}
    other = {"payload": {"message": "OTHER-SESSION"}}
    try:
        for key, update in ((queue.key, first), (other_key, other)):
            queue.client.rpush(key, json.dumps(update))
            queue.client.expire(key, 60)
        assert context_updates.drain_rca_context_updates(queue.user_id, queue.session_id) == [first]
        assert [json.loads(raw) for raw in queue.client.lrange(other_key, 0, -1)] == [other]
    finally:
        queue.client.delete(other_key)


def test_two_consumers_do_not_duplicate_the_batch(context_updates, redis_queue, monkeypatch):
    from redis import Redis

    queue = redis_queue
    updates = [{"payload": {"message": f"alert-{i}"}} for i in range(3)]
    queue.client.rpush(queue.key, *(json.dumps(update) for update in updates))
    queue.client.expire(queue.key, 60)
    clients = [Redis(connection_pool=queue.client.connection_pool) for _ in range(2)]
    local = threading.local()
    ready = threading.Barrier(2, timeout=5)
    monkeypatch.setattr(context_updates, "get_redis_client", lambda: local.client)

    def drain(client):
        local.client = client
        ready.wait()
        return context_updates.drain_rca_context_updates(queue.user_id, queue.session_id)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            batches = list(pool.map(drain, clients))
        assert [update for batch in batches for update in batch] == updates
        assert sorted(len(batch) for batch in batches) == [0, len(updates)]
        assert not queue.client.exists(queue.key)
    finally:
        for client in clients:
            client.close()


def test_foreground_chat_does_not_consume_rca_updates(context_updates, monkeypatch):
    drain = MagicMock()
    monkeypatch.setattr(context_updates, "drain_rca_context_updates", drain)
    assert context_updates.apply_rca_context_updates(SimpleNamespace(is_background=False)) is None
    drain.assert_not_called()


@pytest.fixture
def context_middleware(context_updates, monkeypatch):
    for package in ("langchain", "langgraph", "langchain-core"):
        try:
            version(package)
        except PackageNotFoundError:
            pytest.skip(f"{package} is required for the real graph regression")

    # Only model-size lookup is replaced; graph and middleware behavior are real.
    manager = ModuleType("chat.backend.agent.utils.chat_context_manager")
    manager.ChatContextManager = SimpleNamespace(get_context_limit=lambda _: 128000)
    monkeypatch.setitem(sys.modules, manager.__name__, manager)
    return _load_module(
        monkeypatch,
        "chat.backend.agent.middleware.context_trim",
        SERVER / "chat/backend/agent/middleware/context_trim.py",
    )


@pytest.mark.parametrize("is_background,rca_context,receives_update", [
    (True, {"source": "grafana"}, True),
    (False, None, False),
    (True, None, False),
])
def test_context_survives_later_agent_model_turns(
    context_updates, context_middleware, monkeypatch, is_background, rca_context, receives_update,
):
    from langchain.agents import create_agent
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.tools import tool
    from chat.backend.agent.utils.state import State
    from utils.cloud import cloud_utils

    state = State(question="Investigate synthetic order failures", user_id="test-user",
                  session_id="test-session", is_background=is_background, rca_context=rca_context)
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
                             middleware=[context_middleware.ContextSafetyMiddleware("test-model")])
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
    if not receives_update:
        assert not any(contains_note(messages) for messages in seen)
        assert not contains_note(output["messages"])
        assert not state.rca_ui_updates
        assert pending == [update], "Non-RCA runs must not consume the update"
        return

    assert contains_note(seen[1]), "The update must reach its first eligible request"
    assert len(state.rca_ui_updates) == 1, "One update must produce one UI record"
    assert any(isinstance(m, ToolMessage) and m.content == "SYNTHETIC-EVIDENCE-1" for m in seen[2])
    assert contains_note(seen[2]), "The original context vanished on the following model turn"
    assert contains_note(output["messages"])
    for messages in (seen[1], seen[2], output["messages"]):
        notes = [m for m in messages if isinstance(m, HumanMessage) and note in str(m.content)]
        assert len(notes) == 1, "The same update must not be injected repeatedly"
        call_ids = {call["id"] for m in messages if isinstance(m, AIMessage) for call in m.tool_calls}
        assert all(m.tool_call_id in call_ids for m in messages if isinstance(m, ToolMessage))
    assert not contains_note(state.messages), "The final graph collector owns the outer message append"


def test_context_safety_trim_still_removes_orphaned_tool_results(context_middleware):
    from langchain.agents.middleware.types import ModelRequest
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from langchain_core.messages.utils import count_tokens_approximately

    middleware = context_middleware.ContextSafetyMiddleware("test-model")
    middleware.max_tokens = 20
    messages = [
        HumanMessage(content="Old context " * 2000),
        AIMessage(content="Read evidence", tool_calls=[{
            "name": "read_evidence", "args": {}, "id": "old-call", "type": "tool_call",
        }]),
        ToolMessage(content="Old evidence", tool_call_id="old-call"),
        HumanMessage(content="Current question"),
    ]
    request = ModelRequest(model=MagicMock(), messages=messages)
    captured = []
    response = object()

    async def handler(updated):
        captured.append(updated.messages)
        return response

    assert asyncio.run(middleware.awrap_model_call(request, handler)) is response
    assert len(captured) == 1
    trimmed = captured[0]
    assert count_tokens_approximately(trimmed) <= middleware.max_tokens
    assert trimmed[-1].content == "Current question"
    assert messages[0] not in trimmed
    call_ids = {call["id"] for m in trimmed if isinstance(m, AIMessage) for call in m.tool_calls}
    assert all(m.tool_call_id in call_ids for m in trimmed if isinstance(m, ToolMessage))


@pytest.mark.parametrize("model_kind", ["scripted", "live"])
def test_combined_queue_and_graph_delivery(
    context_updates, context_middleware, redis_queue, monkeypatch, model_kind,
):
    """Exercise enqueue, atomic batches, formatting and graph persistence together."""
    if model_kind == "live" and os.getenv("RCA_TEST_LIVE_MODEL") != "1":
        pytest.skip("Set RCA_TEST_LIVE_MODEL=1 to allow a small paid model test")

    from langchain.agents import create_agent
    from langchain.agents.middleware import AgentMiddleware
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.tools import tool
    from chat.backend.agent.utils.state import State
    from utils.cloud import cloud_utils

    queue = redis_queue
    state = State(question="Investigate a synthetic checkout failure", user_id=queue.user_id,
                  session_id=queue.session_id, is_background=True, rca_context={"source": "grafana"})
    notes = {
        "order_id": "SYNTHETIC-ORDER-42",
        "region": "SYNTHETIC-CA",
        "deployment": "SYNTHETIC-DEPLOY-17",
        "trace_id": "SYNTHETIC-TRACE-9",
    }
    seen = []
    calls = []
    first_batch_ready = False
    late_inserted = False

    def enqueue(source, payload):
        assert context_updates.enqueue_rca_context_update(
            queue.user_id, queue.session_id, source, payload,
        ), "The synthetic update must be accepted"

    pipeline = queue.client.pipeline

    def interleave_pipeline(*args, **kwargs):
        batch = pipeline(*args, **kwargs)
        execute = batch.execute

        def execute_batch(*a, **kw):
            nonlocal late_inserted
            result = execute(*a, **kw)
            if first_batch_ready and not late_inserted:
                late_inserted = True
                enqueue("jenkins", {"title": "Deployment", "body": f"deployment={notes['deployment']}"})
            return result

        batch.execute = execute_batch
        return batch

    monkeypatch.setattr(queue.client, "pipeline", interleave_pipeline)

    @tool
    def inspect_demo(step: int, order_id: str = "", region: str = "",
                     deployment: str = "", trace_id: str = "") -> str:
        """Read synthetic evidence. Run steps 1, 2 and 3 in order, using context identifiers."""
        nonlocal first_batch_ready
        calls.append(dict(step=step, order_id=order_id, region=region,
                          deployment=deployment, trace_id=trace_id))
        if step == 1:
            enqueue("grafana", {"title": "Order", "message": f"order_id={notes['order_id']}"})
            enqueue("pagerduty", {"event": {"data": {
                "title": "Region", "body": {"details": f"region={notes['region']}"},
            }}})
            first_batch_ready = True
        elif step == 2:
            enqueue("manual_merge", {"title": "Trace", "merged_from_incident": "synthetic",
                                      "body": f"## Summary\ntrace_id={notes['trace_id']}"})
        return f"SYNTHETIC-EVIDENCE-{step}"

    class CaptureRequests(AgentMiddleware):
        async def awrap_model_call(self, request, handler):
            seen.append(list(request.messages))
            return await handler(request)

    class ScriptedModel(BaseChatModel):
        @property
        def _llm_type(self):
            return "rca-combined-regression-scripted"

        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            step = len(seen)
            args = {"step": step}
            text = "\n".join(str(m.content) for m in messages if isinstance(m, HumanMessage))
            for key in notes:
                match = re.search(rf"\b{key}=(SYNTHETIC-[A-Z0-9-]+)", text)
                if match:
                    args[key] = match.group(1)
            tool_calls = [] if step == 4 else [{
                "name": "inspect_demo", "args": args, "id": f"combined-{step}", "type": "tool_call",
            }]
            return ChatResult(generations=[ChatGeneration(message=AIMessage(
                content="Synthetic investigation complete" if not tool_calls else "Read synthetic evidence",
                tool_calls=tool_calls,
            ))])

    model = ScriptedModel()
    if model_kind == "live":
        from chat.backend.agent.providers import create_chat_model

        model = create_chat_model(
            model=os.environ["MAIN_MODEL"], provider_mode=os.environ["LLM_PROVIDER_MODE"],
            temperature=0, streaming=False, max_tokens=4096, timeout=45.0,
        )

    async def run():
        agent = create_agent(
            model=model, tools=[inspect_demo],
            middleware=[context_middleware.ContextSafetyMiddleware("test-model"), CaptureRequests()],
            system_prompt=(
                "This is a synthetic integration test, not a production incident. "
                "Call inspect_demo exactly three times in order: step 1 with no identifiers; "
                "step 2 with order_id and region from the new context; step 3 with all four "
                "identifiers from context: order_id, region, deployment and trace_id. "
                "Read identifiers only from supplied context; do not invent them. "
                "After step 3, summarize briefly and state that all evidence is synthetic."
            ),
        )
        result = None
        async for event in agent.astream_events(
            {"messages": [HumanMessage(content=state.question)]},
            config={"recursion_limit": 20}, version="v2",
        ):
            if event["event"] == "on_chain_end" and event["name"] == "LangGraph":
                result = event["data"]["output"]
        return result

    token = cloud_utils._state_var.set(state)
    try:
        output = asyncio.run(asyncio.wait_for(run(), timeout=180))
    finally:
        cloud_utils._state_var.reset(token)

    assert late_inserted
    assert len(seen) == 4
    assert [call["step"] for call in calls] == [1, 2, 3]
    assert calls[1]["order_id"] == notes["order_id"]
    assert calls[1]["region"] == notes["region"]
    assert {key: calls[2][key] for key in notes} == notes
    assert not queue.client.exists(queue.key)
    assert [update["update_count"] for update in state.rca_ui_updates] == [2, 2]
    assert not state.messages, "Only the final graph collector should append outer messages"
    for index, messages in enumerate([*seen, output["messages"]]):
        for key, value in notes.items():
            actual = sum(value in str(m.content) for m in messages if isinstance(m, HumanMessage))
            expected = 0 if index == 0 or (index == 1 and key in ("deployment", "trace_id")) else 1
            assert actual == expected, f"Unexpected delivery count for {key} at snapshot {index}"
        call_ids = {call["id"] for m in messages if isinstance(m, AIMessage) for call in m.tool_calls}
        assert all(m.tool_call_id in call_ids for m in messages if isinstance(m, ToolMessage))
    evidence = [m.content for m in output["messages"] if isinstance(m, ToolMessage)]
    assert evidence == [f"SYNTHETIC-EVIDENCE-{step}" for step in (1, 2, 3)]
    if model_kind == "live":
        print(json.dumps({"model": os.environ["MAIN_MODEL"], "tool_steps": [c["step"] for c in calls],
                          "model_requests": len(seen), "context_batches": [2, 2],
                          "all_context_retained": True, "synthetic_evidence_only": True}))
