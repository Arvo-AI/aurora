"""
Universal tool output capping for Aurora.

Prevents large tool outputs from entering the LLM context by capping them
at the source — before LangChain adds them to the ReAct message list.

Small outputs pass through unchanged. Medium outputs are summarized via LLM.
Huge outputs are truncated first, then summarized.

The summary itself comes from ``tool_output_summary.summarize_tool_output_once``,
which runs the cheap summarization model once per distinct output (head + tail
trimmed, output capped) and shares the result with ``ToolContextCapture``, so a
large tool result is no longer compressed twice per call.
"""

import logging
import os
from typing import Optional

from .tool_output_summary import MAX_SUMMARIZATION_INPUT_CHARS  # noqa: F401  (re-exported)

logger = logging.getLogger(__name__)

# Outputs below this are passed through unchanged (~10K tokens)
PASS_THROUGH_CHARS = 40_000

# Background RCA runs pass a lower threshold (RCA_TOOL_OUTPUT_PASS_THROUGH_CHARS,
# default 20K chars ≈ 5K tokens): every ReAct turn re-reads every earlier tool
# output, so a 240-iteration investigation pays for each oversized output dozens
# of times, while a one-off summary costs a single cheap-tier call. Interactive
# chat keeps PASS_THROUGH_CHARS.
DEFAULT_BACKGROUND_PASS_THROUGH_CHARS = 20_000


def background_pass_through_chars() -> int:
    """``RCA_TOOL_OUTPUT_PASS_THROUGH_CHARS`` for background runs; unset / empty /
    junk / non-positive values mean the 20K default."""
    raw = (os.getenv("RCA_TOOL_OUTPUT_PASS_THROUGH_CHARS") or "").strip()
    if not raw:
        return DEFAULT_BACKGROUND_PASS_THROUGH_CHARS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_BACKGROUND_PASS_THROUGH_CHARS
    return value if value > 0 else DEFAULT_BACKGROUND_PASS_THROUGH_CHARS


def cap_tool_output(
    output: str, tool_name: str = "unknown", pass_through_chars: Optional[int] = None
) -> str:
    """Cap a tool output to a reasonable size for LLM context.

    - <= threshold chars (default 40K): pass through as-is
    - > threshold chars: summarize via LLM (``summarize_tool_output_once`` cuts the
      input at ``MAX_SUMMARIZATION_INPUT_CHARS`` and keeps a head + tail window)

    Args:
        output: Raw tool output string.
        tool_name: Tool name for logging.
        pass_through_chars: Threshold override (background RCA passes
            ``background_pass_through_chars()``); ``None`` keeps ``PASS_THROUGH_CHARS``.

    Returns:
        The original output (if small) or a summarized version.
    """
    threshold = (
        pass_through_chars
        if pass_through_chars is not None and pass_through_chars > 0
        else PASS_THROUGH_CHARS
    )
    if len(output) <= threshold:
        return output

    logger.info(
        f"[ToolOutputCap] {tool_name}: output is {len(output):,} chars, "
        f"threshold is {threshold:,} — summarizing"
    )

    try:
        from .tool_output_summary import summarize_tool_output_once
        from utils.cloud.cloud_utils import get_user_context

        ctx = get_user_context()

        # The raw output goes in unchanged: the shared summarizer trims it and
        # keys its result cache on the full text, so ToolContextCapture's call
        # for the same output (made just before this one) is reused here.
        summary = summarize_tool_output_once(
            output,
            tool_name=tool_name,
            user_id=ctx.get("user_id"),
            session_id=ctx.get("session_id"),
        )
        summary_with_marker = summary + "\n\n[Summarized from larger output]"
        logger.info(
            f"[ToolOutputCap] {tool_name}: summarized {len(output):,} chars -> "
            f"{len(summary_with_marker):,} chars"
        )
        return summary_with_marker

    except Exception as e:
        logger.error(f"[ToolOutputCap] {tool_name}: summarization failed ({e}), truncating")
        # Fallback: hard truncate to pass-through size
        return (
            output[:threshold]
            + "\n\n[Output truncated — summarization failed]"
        )
