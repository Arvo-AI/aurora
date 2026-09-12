"""
Universal tool output capping for Aurora.

Prevents large tool outputs from entering the LLM context by capping them
at the source — before LangChain adds them to the ReAct message list.

Small outputs pass through unchanged. Medium outputs are summarized via LLM.
Huge outputs are truncated first, then summarized.
"""

import logging
import os

logger = logging.getLogger(__name__)


def _positive_int_env(name: str, default: int) -> int:
    """Read a positive integer environment variable or fail at startup."""
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer, got {raw_value!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer, got {raw_value!r}")
    return value


# Outputs below this are passed through unchanged (~10K tokens)
PASS_THROUGH_CHARS = _positive_int_env("TOOL_OUTPUT_PASS_THROUGH_CHARS", 40_000)

# Outputs above this are truncated before summarization
MAX_SUMMARIZATION_INPUT_CHARS = _positive_int_env(
    "TOOL_OUTPUT_MAX_SUMMARIZATION_INPUT_CHARS", 400_000
)

if PASS_THROUGH_CHARS >= MAX_SUMMARIZATION_INPUT_CHARS:
    raise ValueError(
        "TOOL_OUTPUT_PASS_THROUGH_CHARS must be smaller than "
        "TOOL_OUTPUT_MAX_SUMMARIZATION_INPUT_CHARS"
    )


def cap_tool_output(output: str, tool_name: str = "unknown") -> str:
    """Cap a tool output to a reasonable size for LLM context.

    - <= PASS_THROUGH_CHARS: pass through as-is
    - up to MAX_SUMMARIZATION_INPUT_CHARS: summarize via LLM
    - above MAX_SUMMARIZATION_INPUT_CHARS: truncate, then summarize

    Args:
        output: Raw tool output string.
        tool_name: Tool name for logging.

    Returns:
        The original output (if small) or a summarized version.
    """
    if len(output) <= PASS_THROUGH_CHARS:
        return output

    logger.info(
        f"[ToolOutputCap] {tool_name}: output is {len(output):,} chars, "
        f"threshold is {PASS_THROUGH_CHARS:,} — summarizing"
    )

    # Truncate before summarization if extremely large
    content_to_summarize = output
    if len(output) > MAX_SUMMARIZATION_INPUT_CHARS:
        logger.info(
            f"[ToolOutputCap] {tool_name}: truncating from {len(output):,} to "
            f"{MAX_SUMMARIZATION_INPUT_CHARS:,} chars before summarization"
        )
        content_to_summarize = (
            output[:MAX_SUMMARIZATION_INPUT_CHARS]
            + "\n\n[Output truncated before summarization]"
        )

    try:
        from ..llm import LLMManager, ModelConfig
        from utils.cloud.cloud_utils import get_user_context

        ctx = get_user_context()

        llm = LLMManager()
        summary = llm.summarize(
            content_to_summarize,
            model=ModelConfig.TOOL_OUTPUT_SUMMARIZATION_MODEL,
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
            output[:PASS_THROUGH_CHARS]
            + "\n\n[Output truncated — summarization failed]"
        )
