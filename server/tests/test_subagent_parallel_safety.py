"""Tests for the LangGraph parallel-safety contract.

Covers:
- subagent subgraph compiled without a checkpointer (per-invocation isolation)
- "subagent" registered exactly once in the main agent graph
- fan_out_node emits one Send per planned sub-agent with unique agent_ids
- MainAgentState.subagent_results uses operator.add as the reducer
"""

import ast
import operator
import os
import sys
import typing
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Ensure server/ is on sys.path
_server_dir = os.path.join(os.path.dirname(__file__), os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))

# Stub heavy third-party packages. pydantic needs real types (not MagicMocks)
# for the AnyMessage hint on the State base class.
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
    sys.modules["langchain_core.messages"] = _msgs

# Pre-stub helper modules so importing the orchestrator nodes module doesn't
# transitively pull real LLM SDKs.
if "chat.backend.agent.llm" not in sys.modules:
    sys.modules["chat.backend.agent.llm"] = MagicMock()
if "chat.backend.agent.providers" not in sys.modules:
    sys.modules["chat.backend.agent.providers"] = MagicMock()
if "chat.backend.agent.utils.persistence.chat_events" not in sys.modules:
    sys.modules["chat.backend.agent.utils.persistence.chat_events"] = MagicMock()

from chat.backend.agent.orchestrator.state import MainAgentState  # noqa: E402


# ---------------------------------------------------------------------------
# Subagent subgraph: no checkpointer
# ---------------------------------------------------------------------------


def test_subagent_subgraph_compiled_without_checkpointer():
    """build_subagent_subgraph().compile() must be called without a checkpointer.

    Verified via AST: the subagent/graph.py source must call ``graph.compile()``
    with no positional args and no ``checkpointer=`` kwarg.
    """
    src_path = (
        Path(__file__).resolve().parent.parent
        / "chat" / "backend" / "agent" / "subagent" / "graph.py"
    )
    tree = ast.parse(src_path.read_text())

    # find build_subagent_subgraph and inspect its compile() call
    found_compile = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "build_subagent_subgraph":
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "compile"
                ):
                    found_compile = True
                    # no positional args + no checkpointer kwarg
                    assert not sub.args, (
                        "subagent compile() must take no positional args "
                        "(no checkpointer)"
                    )
                    for kw in sub.keywords:
                        assert kw.arg != "checkpointer", (
                            "subagent compile() must not pass checkpointer= "
                            "(parent-only state)"
                        )
    assert found_compile, "expected a .compile() call in build_subagent_subgraph"


# ---------------------------------------------------------------------------
# Main graph: "subagent" node registered exactly once
# ---------------------------------------------------------------------------


def test_subagent_uniquely_named_in_main_graph():
    """The main graph's add_node('subagent', ...) must appear exactly once."""
    src_path = (
        Path(__file__).resolve().parent.parent
        / "chat" / "backend" / "agent" / "orchestrator" / "graph.py"
    )
    tree = ast.parse(src_path.read_text())

    count = 0
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_node"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "subagent"
        ):
            count += 1
    assert count == 1, f"'subagent' must be registered exactly once, got {count}"


# ---------------------------------------------------------------------------
# fan_out_node: per-branch state isolation, unique agent_ids
# ---------------------------------------------------------------------------


def test_send_branch_state_isolated():
    """fan_out_node emits N Sends; each branch_state has a UNIQUE agent_id."""
    # Stub langgraph.constants.Send so we can introspect the calls.
    captured_sends = []

    class _FakeSend:
        def __init__(self, node, state):
            self.node = node
            self.state = state
            captured_sends.append(self)

    sys.modules["langgraph.constants"].Send = _FakeSend

    # Reload nodes so it re-binds Send to our fake.
    import importlib

    from chat.backend.agent.orchestrator import nodes as nodes_mod
    importlib.reload(nodes_mod)

    state = MainAgentState(
        question="q",
        user_id="u",
        org_id="o",
        session_id="s",
        complexity="fan_out_warranted",
        plan={
            "selected": [
                {"purpose": "a"},
                {"purpose": "b"},
                {"purpose": "c"},
            ]
        },
    )

    sends = nodes_mod.route_after_plan(state)
    assert isinstance(sends, list) and len(sends) == 3
    agent_ids = [s.state["agent_id"] for s in sends]
    assert len(set(agent_ids)) == 3, f"agent_ids must be unique, got {agent_ids}"
    for ai in agent_ids:
        assert ai.startswith("sub-")


# ---------------------------------------------------------------------------
# MainAgentState.subagent_results uses operator.add as reducer
# ---------------------------------------------------------------------------


def test_subagent_results_reducer_merges():
    """The Annotated metadata on subagent_results must include operator.add.

    Try pydantic's model_fields metadata first, then fall back to typing.get_type_hints.
    """
    metadata: tuple = ()
    try:
        field = MainAgentState.model_fields["subagent_results"]
        metadata = tuple(getattr(field, "metadata", ()) or ())
    except Exception:
        metadata = ()

    if operator.add not in metadata:
        try:
            hints = typing.get_type_hints(MainAgentState, include_extras=True)
            sub_hint = hints["subagent_results"]
            metadata = tuple(getattr(sub_hint, "__metadata__", ()) or ())
        except Exception:
            pass

    assert operator.add in metadata, (
        f"subagent_results must use operator.add reducer; "
        f"got metadata={metadata}"
    )
