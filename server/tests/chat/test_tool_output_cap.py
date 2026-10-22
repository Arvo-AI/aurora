"""Tests the cap that prevents oversized tool outputs (kubectl logs, AWS
describes, Datadog event dumps, etc.) from blowing the LLM context
window when an agent step returns a huge payload. Pins the behaviour --
pass through at or under the pass-through threshold, summarize above it
through the shared ``summarize_tool_output_once`` entry point (which owns
the input ceiling and the head + tail trim), and the graceful fallback
when the summarizer itself fails.
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
from chat.backend.agent.utils.tool_output_summary import (  # noqa: E402
    TOOL_OUTPUT_SUMMARY_INPUT_CHARS,
)


_SUMMARIZE_MARKER = "\n\n[Summarized from larger output]"
_FALLBACK_MARKER = "\n\n[Output truncated — summarization failed]"


@pytest.fixture()
def fake_summarizer(monkeypatch):
    """Stub the lazy ``.tool_output_summary`` and ``utils.cloud.cloud_utils`` imports."""
    summarize = MagicMock(name="summarize_tool_output_once", return_value="SUMMARY")

    fake_summary_module = types.ModuleType("chat.backend.agent.utils.tool_output_summary")
    fake_summary_module.summarize_tool_output_once = summarize
    fake_summary_module.MAX_SUMMARIZATION_INPUT_CHARS = MAX_SUMMARIZATION_INPUT_CHARS

    fake_cloud_module = types.ModuleType("utils.cloud.cloud_utils")
    fake_cloud_module.get_user_context = MagicMock(
        return_value={"user_id": "u-1", "session_id": "s-1"},
    )

    monkeypatch.setitem(sys.modules, "chat.backend.agent.utils.tool_output_summary", fake_summary_module)
    monkeypatch.setitem(sys.modules, "utils.cloud.cloud_utils", fake_cloud_module)

    return summarize


# ---------------------------------------------------------------------------
# Pass-through
# ---------------------------------------------------------------------------


class TestPassThrough:
    """``len <= PASS_THROUGH_CHARS`` returns unchanged, no LLM call."""

    @pytest.mark.parametrize("size", [0, 1, 100, PASS_THROUGH_CHARS - 1])
    def test_short_outputs_pass_through_unchanged(self, fake_summarizer, size):
        payload = "x" * size

        result = cap_tool_output(payload, tool_name="t")

        assert result == payload
        fake_summarizer.assert_not_called()

    def test_exact_threshold_passes_through_unchanged(self, fake_summarizer):
        payload = "y" * PASS_THROUGH_CHARS

        result = cap_tool_output(payload, tool_name="t")

        assert result == payload
        fake_summarizer.assert_not_called()


# ---------------------------------------------------------------------------
# Summarization invoked
# ---------------------------------------------------------------------------


class TestSummarizationInvoked:
    """``len > PASS_THROUGH_CHARS`` -> one shared summarization, append marker."""

    def test_one_char_over_threshold_invokes_summarization(self, fake_summarizer):
        payload = "z" * (PASS_THROUGH_CHARS + 1)

        result = cap_tool_output(payload, tool_name="t")

        fake_summarizer.assert_called_once()
        assert result.endswith(_SUMMARIZE_MARKER)
        assert "SUMMARY" in result
        assert payload not in result

    @pytest.mark.parametrize(
        "size",
        [PASS_THROUGH_CHARS + 1, MAX_SUMMARIZATION_INPUT_CHARS, MAX_SUMMARIZATION_INPUT_CHARS * 5],
    )
    def test_summarizer_receives_the_raw_output(self, fake_summarizer, size):
        """The cap hands the untouched text over: the shared summarizer owns the
        input ceiling and the head + tail trim, and keys its result cache on the
        full text so ToolContextCapture's earlier call for the same output hits."""
        payload = "a" * size

        cap_tool_output(payload, tool_name="t")

        sent = fake_summarizer.call_args.args[0]
        assert sent is payload

    def test_summarizer_called_with_tool_name_and_user_context(self, fake_summarizer):
        payload = "b" * (PASS_THROUGH_CHARS + 10)

        cap_tool_output(payload, tool_name="kubectl_logs")

        kwargs = fake_summarizer.call_args.kwargs
        assert kwargs["tool_name"] == "kubectl_logs"
        assert kwargs["user_id"] == "u-1"
        assert kwargs["session_id"] == "s-1"


# ---------------------------------------------------------------------------
# Summarization failure fallback
# ---------------------------------------------------------------------------


class TestSummarizationFailureFallback:
    """Summarizer raises -> hard-truncate to ``PASS_THROUGH_CHARS`` + marker."""

    def test_summarize_raises_returns_truncated_with_marker(self, fake_summarizer):
        fake_summarizer.side_effect = RuntimeError("LLM down")
        payload = "f" * (PASS_THROUGH_CHARS + 50_000)

        result = cap_tool_output(payload, tool_name="t")

        assert result.startswith("f" * PASS_THROUGH_CHARS)
        assert result.endswith(_FALLBACK_MARKER)
        assert len(result) == PASS_THROUGH_CHARS + len(_FALLBACK_MARKER)

    def test_fallback_logs_error_but_does_not_raise(self, fake_summarizer, caplog):
        fake_summarizer.side_effect = ValueError("boom")
        payload = "g" * (PASS_THROUGH_CHARS + 1)

        with caplog.at_level(logging.ERROR, logger=tool_output_cap.logger.name):
            result = cap_tool_output(payload, tool_name="my_tool")

        assert result.endswith(_FALLBACK_MARKER)
        assert any("summarization failed" in rec.message for rec in caplog.records)
        assert any("my_tool" in rec.message for rec in caplog.records)

    def test_fallback_runs_when_lazy_import_fails(self, monkeypatch):
        broken = types.ModuleType("chat.backend.agent.utils.tool_output_summary")

        def _explode(name):
            raise ImportError("provider package missing")

        broken.__getattr__ = _explode  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "chat.backend.agent.utils.tool_output_summary", broken)

        payload = "h" * (PASS_THROUGH_CHARS + 1)

        result = cap_tool_output(payload, tool_name="t")

        assert result.endswith(_FALLBACK_MARKER)
        assert result.startswith("h" * PASS_THROUGH_CHARS)


# ---------------------------------------------------------------------------
# Threshold constants
# ---------------------------------------------------------------------------


class TestThresholdInvariants:
    """Relationships between thresholds; the values themselves are tunable."""

    def test_pass_through_below_max_summarization(self):
        assert PASS_THROUGH_CHARS < MAX_SUMMARIZATION_INPUT_CHARS

    def test_pass_through_below_summary_window(self):
        """An output that needs summarizing must not be trimmed below the point
        where passing it through would have been cheaper."""
        assert PASS_THROUGH_CHARS < TOOL_OUTPUT_SUMMARY_INPUT_CHARS

    def test_summary_window_within_the_input_ceiling(self):
        assert TOOL_OUTPUT_SUMMARY_INPUT_CHARS <= MAX_SUMMARIZATION_INPUT_CHARS

    def test_input_ceiling_stays_inside_the_redaction_scan_window(self):
        """Everything the summarizer sees must have been redaction-scanned
        (utils/security/output_redaction.py scans the first MAX_SCAN_CHARS)."""
        from utils.security.output_redaction import MAX_SCAN_CHARS

        assert MAX_SUMMARIZATION_INPUT_CHARS < MAX_SCAN_CHARS


# ---------------------------------------------------------------------------
# Background RCA threshold (RCA_TOOL_OUTPUT_PASS_THROUGH_CHARS)
# ---------------------------------------------------------------------------


class TestBackgroundThreshold:
    """Background runs pass a lower ``pass_through_chars``; interactive chat keeps
    the module default. The override must drive both the summarize decision and the
    truncation fallback."""

    def test_default_background_threshold_is_lower_than_interactive(self, monkeypatch):
        monkeypatch.delenv("RCA_TOOL_OUTPUT_PASS_THROUGH_CHARS", raising=False)
        assert tool_output_cap.background_pass_through_chars() == 20_000
        assert tool_output_cap.background_pass_through_chars() < PASS_THROUGH_CHARS

    @pytest.mark.parametrize("raw", ["", "   ", "abc", "0", "-5"])
    def test_invalid_env_values_mean_the_default(self, monkeypatch, raw):
        monkeypatch.setenv("RCA_TOOL_OUTPUT_PASS_THROUGH_CHARS", raw)
        assert tool_output_cap.background_pass_through_chars() == 20_000

    def test_env_override_is_honoured(self, monkeypatch):
        monkeypatch.setenv("RCA_TOOL_OUTPUT_PASS_THROUGH_CHARS", "12000")
        assert tool_output_cap.background_pass_through_chars() == 12_000

    def test_override_summarizes_outputs_the_default_would_pass_through(self, fake_summarizer):
        payload = "k" * 25_000  # under 40K, over the 20K background threshold
        assert cap_tool_output(payload, tool_name="t") == payload
        fake_summarizer.assert_not_called()

        result = cap_tool_output(payload, tool_name="t", pass_through_chars=20_000)
        fake_summarizer.assert_called_once()
        assert result.endswith(_SUMMARIZE_MARKER)

    def test_override_at_or_below_threshold_passes_through(self, fake_summarizer):
        payload = "m" * 20_000
        assert cap_tool_output(payload, tool_name="t", pass_through_chars=20_000) == payload
        fake_summarizer.assert_not_called()

    def test_fallback_truncates_to_the_override(self, fake_summarizer):
        fake_summarizer.side_effect = RuntimeError("LLM down")
        payload = "n" * 30_000
        result = cap_tool_output(payload, tool_name="t", pass_through_chars=20_000)
        assert result.startswith("n" * 20_000)
        assert len(result) == 20_000 + len(_FALLBACK_MARKER)

    @pytest.mark.parametrize("override", [None, 0, -1])
    def test_none_or_non_positive_override_keeps_the_default(self, fake_summarizer, override):
        payload = "p" * 25_000
        assert cap_tool_output(payload, tool_name="t", pass_through_chars=override) == payload
        fake_summarizer.assert_not_called()
