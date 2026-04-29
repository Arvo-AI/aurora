"""Tests for failure paths in the orchestrator: replan caps, validation errors."""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure server/ is on sys.path
_server_dir = os.path.join(os.path.dirname(__file__), os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))

# Stub heavy third-party packages. pydantic needs real types (not MagicMocks)
# for the AnyMessage/AIMessage hints on the State base class.
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
    class _FakeAIMessage:
        def __init__(self, content="", **kwargs):
            self.content = content
    _msgs.AIMessage = _FakeAIMessage
    _msgs.HumanMessage = _FakeAIMessage
    _msgs.SystemMessage = _FakeAIMessage
    _msgs.ToolMessage = _FakeAIMessage
    sys.modules["langchain_core.messages"] = _msgs

if "chat.backend.agent.llm" not in sys.modules:
    _llm_stub = MagicMock()
    _llm_stub.resolve_role_model = MagicMock(return_value=("anthropic", "claude-sonnet"))
    _llm_stub.get_token_spend = MagicMock(return_value=0)
    sys.modules["chat.backend.agent.llm"] = _llm_stub
if "chat.backend.agent.providers" not in sys.modules:
    sys.modules["chat.backend.agent.providers"] = MagicMock()
if "chat.backend.agent.utils.persistence.chat_events" not in sys.modules:
    _chat_events_stub = MagicMock()
    _chat_events_stub.record_event = AsyncMock()
    sys.modules["chat.backend.agent.utils.persistence.chat_events"] = _chat_events_stub

from chat.backend.agent.orchestrator.findings_reader import (  # noqa: E402
    FindingsValidationError,
)
from chat.backend.agent.orchestrator.nodes import synthesize_or_replan_node  # noqa: E402
from chat.backend.agent.orchestrator.state import MainAgentState  # noqa: E402
from chat.backend.agent.subagent.state import SubAgentState  # noqa: E402


def _make_state(**overrides):
    base = dict(
        question="why is x failing",
        user_id="user-1",
        org_id="org-1",
        session_id="sess-1",
        incident_id="inc-1",
        multi_agent_config={
            "max_parallel_subagents": 3,
            "max_total_subagents": 5,
            "monthly_token_cap": None,
            "per_rca_token_budget": 1500000,
        },
    )
    base.update(overrides)
    return MainAgentState(**base)


# ---------------------------------------------------------------------------
# _write_findings_node: failed status → SubAgentResult(status=failed)
# ---------------------------------------------------------------------------


def test_failed_subagent_yields_failed_result():
    """A subagent that died before producing analysis yields a failed result."""
    from chat.backend.agent.subagent import graph as graph_mod

    state = SubAgentState(
        agent_id="sub-1",
        parent_agent_id="main",
        purpose="investigate db",
        status="failed",
        error="boom",
    )

    # Force write_findings to also fail so artifact_ref is None — this
    # exercises the "failed AND no artifact" path explicitly.
    with patch.object(
        graph_mod,
        "write_findings",
        side_effect=RuntimeError("storage down"),
    ), patch.object(
        graph_mod, "_update_subagent_run"
    ), patch.object(
        graph_mod, "_record_event_safe", new=AsyncMock()
    ):
        result = asyncio.run(graph_mod._write_findings_node(state))

    assert result["status"] == "failed"
    assert result["findings_artifact_ref"] is None
    sub_results = result["subagent_results"]
    assert len(sub_results) == 1
    assert sub_results[0]["status"] == "failed"


# ---------------------------------------------------------------------------
# synthesize_or_replan_node: handles all-failed runs
# ---------------------------------------------------------------------------


def test_synthesize_handles_failed_run():
    """All-failed sub-agent results: must not crash, must mention 'no findings'."""
    state = _make_state(
        complexity="fan_out_warranted",
        subagent_results=[
            {"agent_id": "sub-1", "status": "failed", "error": "boom",
             "findings_artifact_ref": None, "purpose": "p"},
            {"agent_id": "sub-2", "status": "failed", "error": "kaboom",
             "findings_artifact_ref": None, "purpose": "p"},
        ],
        replan_count=2,  # at cap to avoid replan
    )

    with patch(
        "chat.backend.agent.utils.persistence.chat_events.record_event",
        new=AsyncMock(),
    ), patch(
        "chat.backend.agent.orchestrator.nodes._check_token_budget",
        return_value=(True, 0, 1000000),
    ), patch(
        "chat.backend.agent.orchestrator.nodes._check_monthly_token_cap",
        return_value=(True, None, None),
    ):
        result = asyncio.run(synthesize_or_replan_node(state))

    assert result["next_action"] == "finalize"
    final_text = result["messages"][0].content
    assert "no findings artifact" in final_text or "(no" in final_text


def test_synthesize_continues_on_findings_validation_error():
    """A FindingsValidationError on one ref must not crash synthesis."""
    state = _make_state(
        complexity="fan_out_warranted",
        replan_count=2,
        subagent_results=[
            {"agent_id": "sub-1", "status": "succeeded",
             "findings_artifact_ref": "ref/a.md", "purpose": "p"},
        ],
    )

    with patch(
        "chat.backend.agent.orchestrator.nodes.read_findings",
        side_effect=FindingsValidationError("missing Summary"),
    ), patch(
        "chat.backend.agent.utils.persistence.chat_events.record_event",
        new=AsyncMock(),
    ), patch(
        "chat.backend.agent.orchestrator.nodes._check_token_budget",
        return_value=(True, 0, 1000000),
    ), patch(
        "chat.backend.agent.orchestrator.nodes._check_monthly_token_cap",
        return_value=(True, None, None),
    ):
        result = asyncio.run(synthesize_or_replan_node(state))

    final_text = result["messages"][0].content
    assert "invalid findings" in final_text
    assert result["next_action"] == "finalize"


# ---------------------------------------------------------------------------
# Replan cap = 2
# ---------------------------------------------------------------------------


def test_replan_capped_at_2():
    """At replan_count >= 2, next_action must be 'finalize' even with low findings."""
    state = _make_state(
        complexity="fan_out_warranted",
        replan_count=2,
        subagent_results=[
            {"agent_id": "sub-1", "status": "succeeded",
             "findings_artifact_ref": "ref/a.md", "purpose": "p"},
        ],
    )

    findings = {
        "frontmatter": {"self_assessed_strength": "low"},
        "sections": {"Summary": "weak"},
    }

    with patch(
        "chat.backend.agent.orchestrator.nodes.read_findings",
        return_value=findings,
    ), patch(
        "chat.backend.agent.utils.persistence.chat_events.record_event",
        new=AsyncMock(),
    ), patch(
        "chat.backend.agent.orchestrator.nodes._check_token_budget",
        return_value=(True, 0, 1000000),
    ), patch(
        "chat.backend.agent.orchestrator.nodes._check_monthly_token_cap",
        return_value=(True, None, None),
    ):
        result = asyncio.run(synthesize_or_replan_node(state))

    assert result["next_action"] == "finalize"


def test_high_strength_short_circuits_replan():
    """A single high-strength finding short-circuits to finalize (Layer 4)."""
    state = _make_state(
        complexity="fan_out_warranted",
        replan_count=0,  # plenty of replans available
        subagent_results=[
            {"agent_id": "sub-1", "status": "succeeded",
             "findings_artifact_ref": "ref/a.md", "purpose": "p"},
            {"agent_id": "sub-2", "status": "succeeded",
             "findings_artifact_ref": "ref/b.md", "purpose": "p"},
        ],
    )

    def _read(ref):
        if ref.endswith("a.md"):
            return {
                "frontmatter": {"self_assessed_strength": "high"},
                "sections": {"Summary": "decisive evidence"},
            }
        return {
            "frontmatter": {"self_assessed_strength": "low"},
            "sections": {"Summary": "weak"},
        }

    with patch(
        "chat.backend.agent.orchestrator.nodes.read_findings",
        side_effect=_read,
    ), patch(
        "chat.backend.agent.utils.persistence.chat_events.record_event",
        new=AsyncMock(),
    ), patch(
        "chat.backend.agent.orchestrator.nodes._check_token_budget",
        return_value=(True, 0, 1000000),
    ), patch(
        "chat.backend.agent.orchestrator.nodes._check_monthly_token_cap",
        return_value=(True, None, None),
    ):
        result = asyncio.run(synthesize_or_replan_node(state))

    assert result["next_action"] == "finalize"
