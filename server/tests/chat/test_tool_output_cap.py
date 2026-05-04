"""Tests for chat.backend.agent.utils.tool_output_cap.cap_tool_output.

Pins the three-threshold contract (pass-through, summarize, truncate-then-
summarize) and the fail-safe fallback when summarization raises. An
off-by-one or silently-swallowed error here corrupts the agent's context
window without raising.
"""

import logging
import os
import sys
import types
from unittest.mock import MagicMock

import pytest

_server_dir = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))

from chat.backend.agent.utils import tool_output_cap  # noqa: E402
from chat.backend.agent.utils.tool_output_cap import (  # noqa: E402
    MAX_SUMMARIZATION_INPUT_CHARS,
    PASS_THROUGH_CHARS,
    cap_tool_output,
)


_SUMMARIZE_MARKER = "\n\n[Summarized from larger output]"
_TRUNCATE_BEFORE_MARKER = "\n\n[Output truncated before summarization]"
_FALLBACK_MARKER = "\n\n[Output truncated — summarization failed]"


@pytest.fixture()
def fake_llm(monkeypatch):
    """Stub the lazy ``..llm`` and ``utils.cloud.cloud_utils`` imports."""
    llm_instance = MagicMock(name="LLMManager_instance")
    llm_instance.summarize.return_value = "SUMMARY"

    fake_llm_module = types.ModuleType("chat.backend.agent.llm")
    fake_llm_module.LLMManager = MagicMock(return_value=llm_instance)
    fake_llm_module.ModelConfig = MagicMock(
        TOOL_OUTPUT_SUMMARIZATION_MODEL="test-summarization-model",
    )

    fake_cloud_module = types.ModuleType("utils.cloud.cloud_utils")
    fake_cloud_module.get_user_context = MagicMock(
        return_value={"user_id": "u-1", "session_id": "s-1"},
    )

    monkeypatch.setitem(sys.modules, "chat.backend.agent.llm", fake_llm_module)
    monkeypatch.setitem(sys.modules, "utils.cloud.cloud_utils", fake_cloud_module)

    return llm_instance


# ---------------------------------------------------------------------------
# Pass-through
# ---------------------------------------------------------------------------


class TestPassThrough:
    """``len <= PASS_THROUGH_CHARS`` returns unchanged, no LLM call."""

    @pytest.mark.parametrize("size", [0, 1, 100, PASS_THROUGH_CHARS - 1])
    def test_short_outputs_pass_through_unchanged(self, fake_llm, size):
        payload = "x" * size

        result = cap_tool_output(payload, tool_name="t")

        assert result == payload
        fake_llm.summarize.assert_not_called()

    def test_exact_threshold_passes_through_unchanged(self, fake_llm):
        payload = "y" * PASS_THROUGH_CHARS

        result = cap_tool_output(payload, tool_name="t")

        assert result == payload
        fake_llm.summarize.assert_not_called()


# ---------------------------------------------------------------------------
# Summarization invoked
# ---------------------------------------------------------------------------


class TestSummarizationInvoked:
    """``PASS_THROUGH_CHARS < len <= MAX_*`` -> summarize, append marker."""

    def test_one_char_over_threshold_invokes_summarization(self, fake_llm):
        payload = "z" * (PASS_THROUGH_CHARS + 1)

        result = cap_tool_output(payload, tool_name="t")

        fake_llm.summarize.assert_called_once()
        assert result.endswith(_SUMMARIZE_MARKER)
        assert "SUMMARY" in result

    def test_summarize_receives_full_input_when_under_truncation_limit(self, fake_llm):
        payload = "a" * MAX_SUMMARIZATION_INPUT_CHARS

        cap_tool_output(payload, tool_name="t")

        sent = fake_llm.summarize.call_args.args[0]
        assert sent == payload
        assert _TRUNCATE_BEFORE_MARKER not in sent

    def test_summarize_called_with_configured_model_and_user_context(self, fake_llm):
        payload = "b" * (PASS_THROUGH_CHARS + 10)

        cap_tool_output(payload, tool_name="t")

        kwargs = fake_llm.summarize.call_args.kwargs
        assert kwargs["model"] == "test-summarization-model"
        assert kwargs["user_id"] == "u-1"
        assert kwargs["session_id"] == "s-1"


# ---------------------------------------------------------------------------
# Pre-summarization truncation
# ---------------------------------------------------------------------------


class TestPreSummarizationTruncation:
    """``len > MAX_SUMMARIZATION_INPUT_CHARS`` -> cut + marker before summarize."""

    def test_oversize_output_is_truncated_before_summarize(self, fake_llm):
        payload = "c" * (MAX_SUMMARIZATION_INPUT_CHARS + 1)

        cap_tool_output(payload, tool_name="t")

        sent = fake_llm.summarize.call_args.args[0]
        assert sent.startswith("c" * MAX_SUMMARIZATION_INPUT_CHARS)
        assert sent.endswith(_TRUNCATE_BEFORE_MARKER)
        assert len(sent) == MAX_SUMMARIZATION_INPUT_CHARS + len(_TRUNCATE_BEFORE_MARKER)

    def test_far_oversize_output_does_not_send_full_payload(self, fake_llm):
        payload = "d" * (MAX_SUMMARIZATION_INPUT_CHARS * 5)

        cap_tool_output(payload, tool_name="t")

        sent = fake_llm.summarize.call_args.args[0]
        assert len(sent) == MAX_SUMMARIZATION_INPUT_CHARS + len(_TRUNCATE_BEFORE_MARKER)

    def test_exact_max_is_not_truncated(self, fake_llm):
        payload = "e" * MAX_SUMMARIZATION_INPUT_CHARS

        cap_tool_output(payload, tool_name="t")

        sent = fake_llm.summarize.call_args.args[0]
        assert sent == payload
        assert _TRUNCATE_BEFORE_MARKER not in sent


# ---------------------------------------------------------------------------
# Summarization failure fallback
# ---------------------------------------------------------------------------


class TestSummarizationFailureFallback:
    """Summarizer raises -> hard-truncate to ``PASS_THROUGH_CHARS`` + marker."""

    def test_summarize_raises_returns_truncated_with_marker(self, fake_llm):
        fake_llm.summarize.side_effect = RuntimeError("LLM down")
        payload = "f" * (PASS_THROUGH_CHARS + 50_000)

        result = cap_tool_output(payload, tool_name="t")

        assert result.startswith("f" * PASS_THROUGH_CHARS)
        assert result.endswith(_FALLBACK_MARKER)
        assert len(result) == PASS_THROUGH_CHARS + len(_FALLBACK_MARKER)

    def test_fallback_logs_error_but_does_not_raise(self, fake_llm, caplog):
        fake_llm.summarize.side_effect = ValueError("boom")
        payload = "g" * (PASS_THROUGH_CHARS + 1)

        with caplog.at_level(logging.ERROR, logger=tool_output_cap.logger.name):
            result = cap_tool_output(payload, tool_name="my_tool")

        assert result.endswith(_FALLBACK_MARKER)
        assert any("summarization failed" in rec.message for rec in caplog.records)
        assert any("my_tool" in rec.message for rec in caplog.records)

    def test_fallback_runs_when_lazy_import_fails(self, monkeypatch):
        broken = types.ModuleType("chat.backend.agent.llm")

        def _explode(name):
            raise ImportError("provider package missing")

        broken.__getattr__ = _explode  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "chat.backend.agent.llm", broken)

        payload = "h" * (PASS_THROUGH_CHARS + 1)

        result = cap_tool_output(payload, tool_name="t")

        assert result.endswith(_FALLBACK_MARKER)
        assert result.startswith("h" * PASS_THROUGH_CHARS)


# ---------------------------------------------------------------------------
# Threshold constants
# ---------------------------------------------------------------------------


class TestThresholdConstants:
    """Pin the documented thresholds; an edit forces this file to update."""

    def test_pass_through_chars_value(self):
        assert PASS_THROUGH_CHARS == 40_000

    def test_max_summarization_input_chars_value(self):
        assert MAX_SUMMARIZATION_INPUT_CHARS == 400_000

    def test_pass_through_below_max_summarization(self):
        assert PASS_THROUGH_CHARS < MAX_SUMMARIZATION_INPUT_CHARS
