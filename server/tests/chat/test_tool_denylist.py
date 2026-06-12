"""Tests for the per-run tool denylist used by background chats.

``State.tool_denylist`` carries a list of tool names that must be removed
from the agent's tool set for a single run (e.g. write/exec tools during
PR change gating). Pins the default-None contract (zero behavior change
for existing callers) and the filtering semantics of
``filter_denied_tools`` — the REAL helper ``agent.agentic_tool_flow``
calls (it lives next to State precisely so tests don't need the heavy
``chat.backend.agent.agent`` import).
"""

import os
import sys
from types import SimpleNamespace

_server_dir = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))

from chat.backend.agent.utils.state import State, filter_denied_tools  # noqa: E402


class TestStateField:
    """State.tool_denylist defaults to None and round-trips."""

    def test_defaults_to_none(self):
        state = State(question="q")
        assert state.tool_denylist is None

    def test_round_trips_value(self):
        state = State(question="q", tool_denylist=["x"])
        assert state.tool_denylist == ["x"]


class TestDenylistFilter:
    """Filtering removes exactly the named tools, leaving the rest."""

    @staticmethod
    def _tools(*names):
        return [SimpleNamespace(name=n) for n in names]

    def test_removes_exactly_the_named_tools(self):
        tools = self._tools("read_logs", "execute_command", "create_pr")

        result = filter_denied_tools(tools, ["execute_command", "create_pr"])

        assert [t.name for t in result] == ["read_logs"]

    def test_none_denylist_leaves_tools_unchanged(self):
        tools = self._tools("read_logs", "execute_command")

        result = filter_denied_tools(tools, None)

        assert result is tools

    def test_empty_denylist_leaves_tools_unchanged(self):
        tools = self._tools("read_logs", "execute_command")

        result = filter_denied_tools(tools, [])

        assert result is tools

    def test_unknown_names_are_ignored(self):
        tools = self._tools("read_logs")

        result = filter_denied_tools(tools, ["not_a_tool"])

        assert [t.name for t in result] == ["read_logs"]

    def test_does_not_mutate_original_list(self):
        """The cached get_cloud_tools() list must never be mutated in place."""
        tools = self._tools("read_logs", "execute_command")

        result = filter_denied_tools(tools, ["execute_command"])

        assert result is not tools
        assert [t.name for t in tools] == ["read_logs", "execute_command"]

    def test_tools_without_name_attribute_are_kept(self):
        odd = object()  # no .name — must not crash, must be kept
        tools = [SimpleNamespace(name="read_logs"), odd]

        result = filter_denied_tools(tools, ["execute_command"])

        assert result == tools
