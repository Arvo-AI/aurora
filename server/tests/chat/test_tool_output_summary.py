"""Shared single summarization of oversized tool outputs
(chat.backend.agent.utils.tool_output_summary).

Both ``cap_tool_output`` and ``ToolContextCapture.capture_tool_end`` compress the
same tool result; this module pins that the model runs once per distinct output,
that failures are never remembered, and that the summarizer only ever sees a
head + tail window taken from inside the redaction-scanned ceiling.
"""

import os
import sys
import types
from unittest.mock import MagicMock

import pytest

_server_dir = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))

from chat.backend.agent.utils import tool_output_summary as tos  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_cache():
    tos.clear_summary_cache()
    yield
    tos.clear_summary_cache()


@pytest.fixture
def fake_llm_module(monkeypatch):
    """Stub the lazy ``chat.backend.agent.llm`` import (summarize_text + ModelConfig)."""
    summarize_text = MagicMock(name="summarize_text", return_value="SUMMARY")
    fake = types.ModuleType("chat.backend.agent.llm")
    fake.summarize_text = summarize_text
    fake.ModelConfig = MagicMock(TOOL_OUTPUT_SUMMARIZATION_MODEL="test-summary-model")
    monkeypatch.setitem(sys.modules, "chat.backend.agent.llm", fake)
    return summarize_text


class TestTrimHeadTail:
    def test_short_text_is_untouched(self):
        assert tos.trim_head_tail("abc", head=10, tail=10) == "abc"

    def test_exact_window_is_untouched(self):
        text = "a" * 20
        assert tos.trim_head_tail(text, head=10, tail=10) == text

    def test_long_text_keeps_head_and_tail_with_marker(self):
        text = "H" * 10 + "M" * 100 + "T" * 10
        out = tos.trim_head_tail(text, head=10, tail=10)
        assert out.startswith("H" * 10)
        assert out.endswith("T" * 10)
        assert "100 chars omitted" in out
        assert "M" not in out.replace("omitted from the middle", "")

    def test_default_window_is_80k(self):
        text = "x" * (tos.TOOL_OUTPUT_SUMMARY_INPUT_CHARS + 1_000)
        out = tos.trim_head_tail(text)
        assert len(out) < len(text)
        assert out.startswith("x" * tos.TOOL_OUTPUT_SUMMARY_HEAD_CHARS)
        assert out.endswith("x" * tos.TOOL_OUTPUT_SUMMARY_TAIL_CHARS)

    def test_marker_never_makes_the_text_longer(self):
        text = "x" * (tos.TOOL_OUTPUT_SUMMARY_INPUT_CHARS + 1)
        assert tos.trim_head_tail(text) is text


class TestPrepareSummaryInput:
    def test_under_window_passes_through(self):
        text = "y" * 50_000
        assert tos.prepare_summary_input(text) is text

    def test_tail_comes_from_inside_the_redaction_scanned_ceiling(self):
        """A multi-MB dump: the tail window must be the tail of the first
        MAX_SUMMARIZATION_INPUT_CHARS chars (what output_redaction scanned), never
        the unscanned real end of the output."""
        scanned = "s" * tos.MAX_SUMMARIZATION_INPUT_CHARS
        unscanned_tail = "Z" * 100_000
        out = tos.prepare_summary_input(scanned + unscanned_tail)
        assert "Z" not in out
        assert out.startswith("s" * tos.TOOL_OUTPUT_SUMMARY_HEAD_CHARS)
        assert out.endswith("[Output truncated before summarization]")
        assert len(out) <= tos.TOOL_OUTPUT_SUMMARY_INPUT_CHARS + 200

    def test_between_window_and_ceiling_is_head_tail_trimmed(self):
        text = "H" * 100_000 + "T" * 100_000
        out = tos.prepare_summary_input(text)
        assert out.startswith("H" * tos.TOOL_OUTPUT_SUMMARY_HEAD_CHARS)
        assert out.endswith("T" * tos.TOOL_OUTPUT_SUMMARY_TAIL_CHARS)
        assert "chars omitted" in out


class TestSummarizeOnce:
    def test_identical_output_is_summarized_once(self, fake_llm_module):
        payload = "k" * 90_000

        first = tos.summarize_tool_output_once(payload, tool_name="a", user_id="u", session_id="s")
        second = tos.summarize_tool_output_once(payload, tool_name="b", user_id="u", session_id="s")

        assert first == second == "SUMMARY"
        fake_llm_module.assert_called_once()

    def test_different_outputs_are_summarized_separately(self, fake_llm_module):
        tos.summarize_tool_output_once("k" * 90_000, tool_name="a")
        tos.summarize_tool_output_once("q" * 90_000, tool_name="a")
        assert fake_llm_module.call_count == 2

    def test_model_sees_the_trimmed_window_with_the_cheap_model_and_no_fallback(self, fake_llm_module):
        payload = "H" * 100_000 + "T" * 100_000

        tos.summarize_tool_output_once(payload, tool_name="t", user_id="u-1", session_id="s-1")

        args, kwargs = fake_llm_module.call_args
        sent = args[0]
        assert len(sent) <= tos.TOOL_OUTPUT_SUMMARY_INPUT_CHARS + 200
        assert sent.startswith("H" * tos.TOOL_OUTPUT_SUMMARY_HEAD_CHARS)
        assert sent.endswith("T" * tos.TOOL_OUTPUT_SUMMARY_TAIL_CHARS)
        assert kwargs["model"] == "test-summary-model"
        assert kwargs["request_type"] == "tool_output_summarization"
        assert kwargs["raise_on_error"] is True
        assert kwargs["user_id"] == "u-1"
        assert kwargs["session_id"] == "s-1"

    def test_failures_propagate_and_are_not_cached(self, fake_llm_module):
        fake_llm_module.side_effect = [RuntimeError("down"), "SUMMARY"]
        payload = "f" * 90_000

        with pytest.raises(RuntimeError):
            tos.summarize_tool_output_once(payload, tool_name="t")
        # The second call must hit the model again (nothing was remembered).
        assert tos.summarize_tool_output_once(payload, tool_name="t") == "SUMMARY"
        assert fake_llm_module.call_count == 2

    def test_cache_is_bounded(self, fake_llm_module):
        for i in range(tos._SUMMARY_CACHE_MAXSIZE + 5):
            tos.summarize_tool_output_once(f"payload-{i}", tool_name="t")
        assert len(tos._summary_cache) == tos._SUMMARY_CACHE_MAXSIZE

    @pytest.mark.parametrize("empty", ["", "   \n", None])
    def test_empty_summary_is_a_failure_and_is_not_cached(self, fake_llm_module, empty):
        """Review B3-01: a model whose output budget was exhausted (thinking tokens,
        truncation) can return nothing. That must not replace the tool output the
        agent sees, nor be remembered for the second consumer — it propagates like
        any summarizer error so each caller applies its own truncation fallback."""
        fake_llm_module.side_effect = [empty, "SUMMARY"]
        payload = "e" * 90_000

        with pytest.raises(ValueError, match="empty summary"):
            tos.summarize_tool_output_once(payload, tool_name="t")
        assert len(tos._summary_cache) == 0
        assert tos.summarize_tool_output_once(payload, tool_name="t") == "SUMMARY"
        assert fake_llm_module.call_count == 2
