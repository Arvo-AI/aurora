"""Tests for the orchestrator planner: plan_node, route_after_plan, FanOutPlan."""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure server/ is on sys.path
_server_dir = os.path.join(os.path.dirname(__file__), os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))

# Stub heavy third-party packages that aren't installed in the test env.
# pydantic introspects type hints, so AnyMessage / AIMessage need to be real
# classes (not MagicMock instances) for State subclasses to import cleanly.
from typing import Any as _AnyType  # noqa: E402

for _pkg in (
    "langgraph.constants",
    "langgraph.graph",
    "boto3",
    "botocore",
    "botocore.config",
    "botocore.exceptions",
):
    if _pkg not in sys.modules:
        sys.modules[_pkg] = MagicMock()

if "langchain_core" not in sys.modules:
    sys.modules["langchain_core"] = MagicMock()
if "langchain_core.messages" not in sys.modules:
    _msgs = MagicMock()
    _msgs.AnyMessage = _AnyType
    class _FakeAIMessage:  # noqa: D401
        def __init__(self, content="", **kwargs):
            self.content = content
    _msgs.AIMessage = _FakeAIMessage
    _msgs.HumanMessage = _FakeAIMessage
    _msgs.SystemMessage = _FakeAIMessage
    _msgs.ToolMessage = _FakeAIMessage
    sys.modules["langchain_core.messages"] = _msgs

# Pre-stub the LLM/provider helpers so plan_node's lazy imports succeed
# without pulling real SDKs.
if "chat.backend.agent.llm" not in sys.modules:
    _llm_stub = MagicMock()
    _llm_stub.resolve_role_model = MagicMock(return_value=("anthropic", "claude-sonnet"))
    _llm_stub.get_token_spend = MagicMock(return_value=0)
    sys.modules["chat.backend.agent.llm"] = _llm_stub
if "chat.backend.agent.providers" not in sys.modules:
    _prov_stub = MagicMock()
    sys.modules["chat.backend.agent.providers"] = _prov_stub
if "chat.backend.agent.utils.persistence.chat_events" not in sys.modules:
    _chat_events_stub = MagicMock()
    _chat_events_stub.record_event = AsyncMock()
    sys.modules["chat.backend.agent.utils.persistence.chat_events"] = _chat_events_stub

from contextlib import ExitStack  # noqa: E402

from chat.backend.agent.orchestrator.nodes import (  # noqa: E402
    FanOutPlan,
    PlannedSubAgent,
    plan_node,
    route_after_plan,
    route_after_synthesize,
)
from chat.backend.agent.orchestrator.state import MainAgentState  # noqa: E402


def _patch_planner_deps(fake_llm, catalog=None):
    """Apply the four patches every plan_node test needs and return an ExitStack."""
    stack = ExitStack()
    stack.enter_context(patch(
        "chat.backend.agent.orchestrator.nodes.get_enabled_catalog",
        return_value=(catalog if catalog is not None else {}),
    ))
    stack.enter_context(patch(
        "chat.backend.agent.llm.resolve_role_model",
        return_value=("anthropic", "claude-sonnet"),
    ))
    stack.enter_context(patch(
        "chat.backend.agent.providers.create_chat_model",
        return_value=fake_llm,
    ))
    stack.enter_context(patch(
        "chat.backend.agent.utils.persistence.chat_events.record_event",
        new=AsyncMock(),
    ))
    return stack


def _make_state(**overrides):
    base = dict(
        question="why is the api 5xx-ing",
        user_id="user-1",
        org_id="org-1",
        session_id="sess-1",
        incident_id="inc-1",
        multi_agent_config={"max_parallel_subagents": 3},
        kb_memory="org_runbook: restart pod first",
    )
    base.update(overrides)
    return MainAgentState(**base)


def _planned(purpose: str, hint: str | None = None) -> PlannedSubAgent:
    return PlannedSubAgent(
        purpose=purpose,
        rationale="r",
        builtin_hint=hint,
        suggested_skill_focus=[],
    )


# ---------------------------------------------------------------------------
# plan_node
# ---------------------------------------------------------------------------


def _fake_llm(plan_obj=None, side_effect=None):
    fake = MagicMock()
    fake.with_structured_output.return_value = fake
    if side_effect is not None:
        fake.ainvoke = AsyncMock(side_effect=side_effect)
    else:
        fake.ainvoke = AsyncMock(return_value=plan_obj)
    return fake


def test_plan_returns_fan_out_plan():
    """Planner LLM emits 3 entries → state.plan["selected"] has 3 entries."""
    state = _make_state()
    plan_obj = FanOutPlan(
        selected=[_planned("a"), _planned("b"), _planned("c")],
        memory_hints_used=[],
        rationale="three angles",
    )
    with _patch_planner_deps(_fake_llm(plan_obj)):
        result = asyncio.run(plan_node(state))
    assert "plan" in result
    assert len(result["plan"]["selected"]) == 3


def test_plan_truncates_to_max_parallel(caplog):
    """Planner emits 5; cap=3 → result truncated to 3 + warning logged."""
    state = _make_state(multi_agent_config={"max_parallel_subagents": 3})
    plan_obj = FanOutPlan(
        selected=[_planned(f"p{i}") for i in range(5)],
        memory_hints_used=[],
        rationale="too many",
    )
    with _patch_planner_deps(_fake_llm(plan_obj)), caplog.at_level("WARNING"):
        result = asyncio.run(plan_node(state))
    assert len(result["plan"]["selected"]) == 3
    assert any("truncating" in rec.message for rec in caplog.records)


def test_plan_failure_returns_empty_plan():
    """LLM raises → empty plan + plan_committed event with status=failed."""
    state = _make_state()
    record_event_mock = AsyncMock()
    fake_llm = _fake_llm(side_effect=RuntimeError("llm down"))
    with patch(
        "chat.backend.agent.orchestrator.nodes.get_enabled_catalog",
        return_value={},
    ), patch(
        "chat.backend.agent.llm.resolve_role_model",
        return_value=("anthropic", "claude-sonnet"),
    ), patch(
        "chat.backend.agent.providers.create_chat_model",
        return_value=fake_llm,
    ), patch(
        "chat.backend.agent.utils.persistence.chat_events.record_event",
        record_event_mock,
    ):
        result = asyncio.run(plan_node(state))

    assert result["plan"]["selected"] == []
    assert record_event_mock.await_count >= 1
    # given the LLM failed, then a failed plan_committed event must be emitted
    failed_calls = [
        c for c in record_event_mock.await_args_list
        if c.kwargs.get("payload", {}).get("status") == "failed"
    ]
    assert failed_calls, "expected a failed plan_committed event"


# ---------------------------------------------------------------------------
# route_after_plan + route_after_synthesize (conditional-edge functions)
# ---------------------------------------------------------------------------


def test_route_after_plan_bypasses_on_trivial():
    from langgraph.graph import END
    state = _make_state(complexity="trivial", plan={"selected": [{"purpose": "p1"}, {"purpose": "p2"}]})
    assert route_after_plan(state) == END


def test_route_after_plan_bypasses_on_zero_or_one_hypothesis():
    from langgraph.graph import END
    state = _make_state(complexity="fan_out_warranted", plan={"selected": []})
    assert route_after_plan(state) == END

    state = _make_state(complexity="fan_out_warranted", plan={"selected": [{"purpose": "p1"}]})
    assert route_after_plan(state) == END


def test_route_after_plan_fans_out_for_two_plus():
    state = _make_state(
        complexity="fan_out_warranted",
        plan={"selected": [{"purpose": "p1"}, {"purpose": "p2"}]},
    )
    result = route_after_plan(state)
    assert isinstance(result, list) and len(result) == 2


def test_route_after_synthesize_finalize_by_default():
    state = _make_state(complexity="fan_out_warranted", plan={"selected": [{"purpose": "p1"}, {"purpose": "p2"}]})
    assert route_after_synthesize(state) == "finalize"


def test_route_after_synthesize_replan_emits_sends():
    state = _make_state(
        complexity="fan_out_warranted",
        plan={"selected": [{"purpose": "p1"}, {"purpose": "p2"}]},
        next_action="fan_out",
    )
    result = route_after_synthesize(state)
    assert isinstance(result, list) and len(result) == 2


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def _capture_prompt():
    """Helper: returns (fake_llm, captured) where captured["prompt"] is set on ainvoke."""
    captured: dict = {}

    async def _ainvoke(prompt):
        captured["prompt"] = prompt
        return FanOutPlan(selected=[], memory_hints_used=[], rationale="")

    fake_llm = MagicMock()
    fake_llm.with_structured_output.return_value = fake_llm
    fake_llm.ainvoke = _ainvoke
    return fake_llm, captured


def test_planner_uses_kb_memory():
    """Rendered prompt must contain the kb_memory text."""
    state = _make_state(kb_memory="ORG_RUNBOOK_TOKEN_xyz")
    fake_llm, captured = _capture_prompt()
    with _patch_planner_deps(fake_llm):
        asyncio.run(plan_node(state))
    assert "ORG_RUNBOOK_TOKEN_xyz" in captured["prompt"]


def test_planner_uses_catalog_as_inspiration():
    """Catalog entries (id + ui_label + purpose_template) appear in prompt."""
    state = _make_state()
    fake_llm, captured = _capture_prompt()
    fake_catalog = {
        "builtin:db": {
            "id": "builtin:db",
            "ui_label": "DB Investigator",
            "purpose_template": "investigate db layer",
        },
        "builtin:net": {
            "id": "builtin:net",
            "ui_label": "Network Investigator",
            "purpose_template": "investigate network paths",
        },
    }
    with _patch_planner_deps(fake_llm, catalog=fake_catalog):
        asyncio.run(plan_node(state))

    prompt = captured["prompt"]
    assert "builtin:db" in prompt
    assert "DB Investigator" in prompt
    assert "builtin:net" in prompt
    assert "investigate network paths" in prompt
