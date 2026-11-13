"""
Universal tool output capping for Aurora.

Prevents large tool outputs from entering the LLM context by capping them
at the source — before LangChain adds them to the ReAct message list.

Small outputs pass through unchanged. Medium outputs are summarized via LLM.
Huge outputs are truncated first, then summarized.
"""

import hashlib
import logging
import threading
from collections import OrderedDict
from typing import Optional

logger = logging.getLogger(__name__)

# Outputs below this are passed through unchanged (~10K tokens)
PASS_THROUGH_CHARS = 40_000

# Outputs above this are truncated before summarization
MAX_SUMMARIZATION_INPUT_CHARS = 400_000

# One tool output is summarized from two places back to back: capture_tool_end
# (for the stored context history) and cap_tool_output (for what the model
# reads). The second caller reuses the first summary instead of paying for a
# second LLM call on the same text.
#
# The two do not receive the same string. cloud_exec and iac_tool capture their
# own output before the tool wrapper redacts it, and cap_tool_output runs after.
# The helper therefore summarizes the redacted copy, whoever calls it: both
# callers then hold one text, and the summarizer never reads a secret the
# wrapper is about to strip.
_SUMMARY_MEMO_MAX_ENTRIES = 32
# LLMManager.summarize() swallows its own errors and returns this instead.
_SUMMARIZATION_ERROR_SUFFIX = "[truncated due to summarization error]"
_summary_memo: "OrderedDict[tuple, str]" = OrderedDict()
_summary_memo_lock = threading.Lock()


def _redacted_copy(output: str) -> str:
    """*output* as the tool wrapper's output redaction leaves it (fail-open, like the wrapper)."""
    try:
        from utils.security.config import config as guardrails_config

        if not guardrails_config.enabled:
            return output
        from utils.security.output_redaction import redact

        redacted, _findings = redact(output)
        return redacted
    except Exception:
        logger.warning("[ToolOutputCap] output redaction unavailable before summarization", exc_info=True)
        return output


def summarize_tool_output_once(
    output: str,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    tool_name: str = "unknown",
) -> str:
    """Summarize a large tool output, once per distinct output per session.

    The redacted copy is what gets summarized and remembered. No audit event
    is emitted here; the tool wrapper emits it when it redacts the result.

    Returns the summary without a marker. Raises if the summarizer cannot be
    loaded or raises; callers keep their own fallback.
    """
    from ..llm import LLMManager, ModelConfig

    output = _redacted_copy(output)

    if user_id is None and session_id is None:
        from utils.cloud.cloud_utils import get_user_context

        ctx = get_user_context()
        user_id, session_id = ctx.get("user_id"), ctx.get("session_id")

    model = ModelConfig.TOOL_OUTPUT_SUMMARIZATION_MODEL
    scope = session_id or user_id
    key = None
    if scope:
        digest = hashlib.sha256(output.encode("utf-8", errors="replace")).hexdigest()
        key = (scope, model, digest)
        with _summary_memo_lock:
            cached = _summary_memo.get(key)
            if cached is not None:
                _summary_memo.move_to_end(key)
        if cached is not None:
            logger.info(f"[ToolOutputCap] {tool_name}: reusing the summary already made for this output")
            return cached

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

    summary = LLMManager().summarize(
        content_to_summarize,
        model=model,
        user_id=user_id,
        session_id=session_id,
    )

    # A failed summary is not kept, so the next caller still gets its own try.
    if key is not None and not summary.rstrip().endswith(_SUMMARIZATION_ERROR_SUFFIX):
        with _summary_memo_lock:
            _summary_memo[key] = summary
            _summary_memo.move_to_end(key)
            while len(_summary_memo) > _SUMMARY_MEMO_MAX_ENTRIES:
                _summary_memo.popitem(last=False)
    return summary


def cap_tool_output(output: str, tool_name: str = "unknown") -> str:
    """Cap a tool output to a reasonable size for LLM context.

    - < 40K chars: pass through as-is
    - 40K - 400K chars: summarize via LLM
    - > 400K chars: truncate to 400K, then summarize

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

    try:
        summary = summarize_tool_output_once(output, tool_name=tool_name)
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
