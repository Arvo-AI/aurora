"""Summarize an oversized tool output once and share the result.

Two consumers compress the same large tool result on every call:

- ``tool_output_cap.cap_tool_output`` — caps what goes back into the ReAct loop
  (anything over 40K chars);
- ``tool_context_capture.ToolContextCapture.capture_tool_end`` — stores a
  compact version for the persisted LLM context (anything over 10K tokens).

Before this module each one built its own ``LLMManager`` and ran its own
summarization call, so a single 200K-char ``kubectl logs`` dump was summarized
twice by the main model with no output cap. :func:`summarize_tool_output_once`
runs the model once per distinct output (keyed by the SHA-256 of the text) on
the cheap ``TOOL_OUTPUT_SUMMARIZATION_MODEL`` with thinking off, trims the
input to a head + tail window first, caps the output, and remembers the result
in a small process-local LRU so the second consumer reuses it. Failures are
never cached — the caller falls back to its own truncation.
"""
from __future__ import annotations

import hashlib
import logging
import threading
from collections import OrderedDict
from typing import Optional

logger = logging.getLogger(__name__)

# Hard ceiling on the slice of a raw tool output that may ever reach the
# summarizer. SECURITY INVARIANT: the redaction pass
# (``utils/security/output_redaction.py``) scans only the first
# ``MAX_SCAN_CHARS`` (512 KB) of a tool output, so every character handed to the
# summarization model must come from inside that window — the head + tail trim
# below is therefore applied to ``output[:MAX_SUMMARIZATION_INPUT_CHARS]``, never
# to the unscanned tail of a multi-MB dump. Keep this constant below
# ``MAX_SCAN_CHARS``.
MAX_SUMMARIZATION_INPUT_CHARS = 400_000
_TRUNCATED_MARKER = "\n\n[Output truncated before summarization]"

# Head + tail window handed to the summarizer (after the ceiling above). Tool
# outputs carry their signal at both ends (headers / first errors and the final
# state), so 40K + 40K chars (~20K tokens) is plenty for a 500-word summary and
# bounds the input cost of every compression call regardless of how large the
# raw dump was.
TOOL_OUTPUT_SUMMARY_HEAD_CHARS = 40_000
TOOL_OUTPUT_SUMMARY_TAIL_CHARS = 40_000
TOOL_OUTPUT_SUMMARY_INPUT_CHARS = TOOL_OUTPUT_SUMMARY_HEAD_CHARS + TOOL_OUTPUT_SUMMARY_TAIL_CHARS

_OMITTED_MARKER = "\n\n[... {omitted:,} chars omitted from the middle of the output ...]\n\n"

_SUMMARY_CACHE_MAXSIZE = 64
_summary_cache: "OrderedDict[str, str]" = OrderedDict()
_summary_cache_lock = threading.Lock()


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def trim_head_tail(
    text: str,
    head: int = TOOL_OUTPUT_SUMMARY_HEAD_CHARS,
    tail: int = TOOL_OUTPUT_SUMMARY_TAIL_CHARS,
) -> str:
    """Keep the first ``head`` and last ``tail`` chars with an omission marker in between.

    Only trims when it actually shortens the text (the marker must not replace a
    middle that is shorter than itself)."""
    if len(text) <= head + tail:
        return text
    omitted = len(text) - head - tail
    marker = _OMITTED_MARKER.format(omitted=omitted)
    if omitted <= len(marker):
        return text
    return text[:head] + marker + text[-tail:]


def prepare_summary_input(output: str) -> str:
    """The exact text the summarizer receives for ``output``.

    1. Cut at ``MAX_SUMMARIZATION_INPUT_CHARS`` (the redaction-scanned window).
    2. Keep head + tail of what is left (``trim_head_tail``).
    """
    if len(output) > MAX_SUMMARIZATION_INPUT_CHARS:
        output = output[:MAX_SUMMARIZATION_INPUT_CHARS] + _TRUNCATED_MARKER
    return trim_head_tail(output)


def _cache_get(key: str) -> Optional[str]:
    with _summary_cache_lock:
        value = _summary_cache.get(key)
        if value is not None:
            _summary_cache.move_to_end(key)
        return value


def _cache_put(key: str, value: str) -> None:
    with _summary_cache_lock:
        _summary_cache[key] = value
        _summary_cache.move_to_end(key)
        while len(_summary_cache) > _SUMMARY_CACHE_MAXSIZE:
            _summary_cache.popitem(last=False)


def clear_summary_cache() -> None:
    """Drop every remembered summary (tests)."""
    with _summary_cache_lock:
        _summary_cache.clear()


def summarize_tool_output_once(
    output: str,
    *,
    tool_name: str = "unknown",
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Return the LLM summary of ``output``, computing it at most once per distinct text.

    Raises whatever the summarizer raises (after logging) so each consumer keeps
    its own fallback truncation; nothing is cached on failure.
    """
    key = _digest(output)
    cached = _cache_get(key)
    if cached is not None:
        logger.info(
            "[ToolOutputSummary] %s: reusing summary for %s chars (digest %s)",
            tool_name, f"{len(output):,}", key[:12],
        )
        return cached

    trimmed = prepare_summary_input(output)
    if len(trimmed) != len(output):
        logger.info(
            "[ToolOutputSummary] %s: trimmed %s -> %s chars (head+tail) before summarizing",
            tool_name, f"{len(output):,}", f"{len(trimmed):,}",
        )

    from chat.backend.agent.llm import ModelConfig, summarize_text

    # raise_on_error: a failed call must propagate (never land in the cache) so
    # each consumer applies its own truncation fallback.
    summary = summarize_text(
        trimmed,
        model=ModelConfig.TOOL_OUTPUT_SUMMARIZATION_MODEL,
        user_id=user_id,
        session_id=session_id,
        request_type="tool_output_summarization",
        raise_on_error=True,
    )
    # An empty summary is a failure, not a result: it would replace the tool
    # output the agent sees with nothing and be remembered for every later
    # consumer of the same text. Treat it like any other summarizer error so the
    # caller's truncation fallback applies and nothing is cached.
    if not (summary or "").strip():
        raise ValueError(
            f"summarization of {len(output):,} chars for {tool_name} returned an empty summary"
        )
    _cache_put(key, summary)
    return summary
