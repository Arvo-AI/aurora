"""Tests the cap that prevents oversized tool outputs (kubectl logs, AWS
describes, Datadog event dumps, etc.) from blowing the LLM context
window when an agent step returns a huge payload. Pins the
three-tier behaviour -- pass through under the pass-through threshold,
summarize between it and the max-summarization-input limit, and
truncate-then-summarize above that limit -- plus the graceful fallback
when the summarizer LLM itself fails.
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
    summarize_tool_output_once,
)


_SUMMARIZE_MARKER = "\n\n[Summarized from larger output]"
_TRUNCATE_BEFORE_MARKER = "\n\n[Output truncated before summarization]"
_FALLBACK_MARKER = "\n\n[Output truncated — summarization failed]"


@pytest.fixture(autouse=True)
def _empty_summary_memo():
    """Summaries are remembered per session; no test may see another's."""
    tool_output_cap._summary_memo.clear()
    yield
    tool_output_cap._summary_memo.clear()


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
        assert payload not in result

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
# One summary per output
# ---------------------------------------------------------------------------


class TestOneSummaryPerOutput:
    """capture_tool_end and cap_tool_output both summarize a tool's output,
    back to back. The second one reuses the first summary."""

    def test_same_output_in_the_same_session_is_summarized_once(self, fake_llm):
        payload = "k" * (PASS_THROUGH_CHARS + 1)

        for_history = summarize_tool_output_once(payload, user_id="u-1", session_id="s-1")
        for_model = cap_tool_output(payload, tool_name="t")

        fake_llm.summarize.assert_called_once()
        assert for_history == "SUMMARY"
        assert for_model == "SUMMARY" + _SUMMARIZE_MARKER

    def test_oversize_output_is_summarized_once(self, fake_llm):
        payload = "n" * (MAX_SUMMARIZATION_INPUT_CHARS + 1)

        summarize_tool_output_once(payload, user_id="u-1", session_id="s-1")
        cap_tool_output(payload, tool_name="t")

        fake_llm.summarize.assert_called_once()
        assert fake_llm.summarize.call_args.args[0].endswith(_TRUNCATE_BEFORE_MARKER)

    def test_another_session_gets_its_own_summary(self, fake_llm):
        payload = "m" * (PASS_THROUGH_CHARS + 1)

        summarize_tool_output_once(payload, user_id="u-1", session_id="s-1")
        summarize_tool_output_once(payload, user_id="u-2", session_id="s-2")

        assert fake_llm.summarize.call_count == 2

    def test_a_different_output_is_summarized_again(self, fake_llm):
        summarize_tool_output_once("p" * (PASS_THROUGH_CHARS + 1), user_id="u-1", session_id="s-1")
        summarize_tool_output_once("q" * (PASS_THROUGH_CHARS + 1), user_id="u-1", session_id="s-1")

        assert fake_llm.summarize.call_count == 2

    def test_a_failed_summary_is_not_reused(self, fake_llm):
        fake_llm.summarize.return_value = "abc... [truncated due to summarization error]"
        payload = "r" * (PASS_THROUGH_CHARS + 1)

        summarize_tool_output_once(payload, user_id="u-1", session_id="s-1")
        summarize_tool_output_once(payload, user_id="u-1", session_id="s-1")

        assert fake_llm.summarize.call_count == 2

    def test_remembered_summaries_are_bounded(self, fake_llm):
        limit = tool_output_cap._SUMMARY_MEMO_MAX_ENTRIES

        for i in range(limit + 5):
            summarize_tool_output_once(f"{i}-" + "s" * PASS_THROUGH_CHARS, user_id="u-1", session_id="s-1")

        assert len(tool_output_cap._summary_memo) == limit


# ---------------------------------------------------------------------------
# A tool that captures its own output
# ---------------------------------------------------------------------------


class TestSelfCapturedOutputSharesOneSummary:
    """cloud_exec and iac_tool hand their output to capture_tool_end before the
    tool wrapper redacts it; cap_tool_output gets the redacted copy. Both must
    end up with one summary, made from the redacted text."""

    _SECRET = "AKIA6ODU7H4ZLXKDNQ3X"

    @pytest.fixture(autouse=True)
    def _guardrails_on(self, monkeypatch):
        monkeypatch.setattr("utils.security.config.config", types.SimpleNamespace(enabled=True))

    def _raw(self):
        # Enough distinct tokens to pass capture_tool_end's 10,000-token threshold too.
        return "AWS_ACCESS_KEY_ID=" + self._SECRET + "\n" + " ".join(str(i) for i in range(30_000))

    def test_capture_then_redact_then_cap_makes_one_llm_call(self, fake_llm):
        from utils.security.output_redaction import redact

        raw = self._raw()
        assert len(raw) > PASS_THROUGH_CHARS
        # 1. The tool captures its own raw output (what capture_tool_end calls).
        for_history = summarize_tool_output_once(raw, user_id="u-1", session_id="s-1", tool_name="cloud_exec")
        # 2. The tool wrapper redacts what the tool returned.
        redacted, findings = redact(raw)
        assert findings and self._SECRET not in redacted
        # 3. The wrapper caps the redacted copy for the model.
        for_model = cap_tool_output(redacted, tool_name="cloud_exec")

        fake_llm.summarize.assert_called_once()
        assert self._SECRET not in fake_llm.summarize.call_args.args[0]
        assert for_model == for_history + _SUMMARIZE_MARKER

    def test_real_capture_tool_end_then_redact_then_cap_makes_one_llm_call(self, fake_llm):
        """Same sequence through the real ToolContextCapture, where the agent stack is installed."""
        capture_mod = pytest.importorskip("chat.backend.agent.utils.tool_context_capture")
        from utils.security.output_redaction import redact

        raw = self._raw()
        capture = capture_mod.ToolContextCapture(session_id="s-1", user_id="u-1")
        call_id = capture.capture_tool_start("cloud_exec", {"provider": "aws", "command": "x"}, tool_call_id="call-1")
        capture.capture_tool_end(call_id, raw, is_error=False)
        redacted, _findings = redact(raw)
        for_model = cap_tool_output(redacted, tool_name="cloud_exec")

        fake_llm.summarize.assert_called_once()
        assert self._SECRET not in fake_llm.summarize.call_args.args[0]
        stored = capture.summarized_tool_results[call_id]["summarized_output"]
        assert stored.startswith("SUMMARY") and for_model.startswith("SUMMARY")

    def test_with_guardrails_off_nothing_is_redacted_and_the_summary_is_still_shared(self, fake_llm, monkeypatch):
        monkeypatch.setattr("utils.security.config.config", types.SimpleNamespace(enabled=False))
        raw = self._raw()

        summarize_tool_output_once(raw, user_id="u-1", session_id="s-1")
        cap_tool_output(raw, tool_name="cloud_exec")  # the wrapper does not redact either

        fake_llm.summarize.assert_called_once()
        assert self._SECRET in fake_llm.summarize.call_args.args[0]

    def test_a_redaction_failure_falls_back_to_the_text_as_given(self, fake_llm, monkeypatch):
        def _boom(_text):
            raise RuntimeError("redaction engine down")

        monkeypatch.setattr("utils.security.output_redaction.redact", _boom)
        payload = "v" * (PASS_THROUGH_CHARS + 1)

        assert cap_tool_output(payload, tool_name="t") == "SUMMARY" + _SUMMARIZE_MARKER


# ---------------------------------------------------------------------------
# Threshold constants
# ---------------------------------------------------------------------------


class TestThresholdInvariants:
    """Relationships between thresholds; the values themselves are tunable."""

    def test_pass_through_below_max_summarization(self):
        assert PASS_THROUGH_CHARS < MAX_SUMMARIZATION_INPUT_CHARS

    def test_summarize_band_is_wide_enough_for_typical_outputs(self):
        """Band width must stay >= 100k chars so typical large outputs aren't pre-truncated."""
        band = MAX_SUMMARIZATION_INPUT_CHARS - PASS_THROUGH_CHARS
        assert band >= 100_000, (
            f"MAX_SUMMARIZATION_INPUT_CHARS - PASS_THROUGH_CHARS = {band:,}, "
            "expected >= 100_000"
        )
