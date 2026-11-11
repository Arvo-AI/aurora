"""Send a timed-out model step again instead of losing the run.

A request timeout turns a stalled model call into an error. Left alone, that
error ends the whole agent run and the tool work before it is thrown away. This
sends only the step that timed out again. Every other error is raised unchanged.
"""

import asyncio
import logging

from langchain.agents.middleware import ModelRetryMiddleware

logger = logging.getLogger(__name__)

# The step is tried three times in all. With the default Gemini timeout that
# is at most half of an investigation's 60 minutes spent on one stalled step.
STEP_TIMEOUT_RETRIES = 2


def _timed_out(exc: Exception) -> bool:
    # The Google client raises the builtin TimeoutError. Providers whose SDK has
    # its own timeout class also do their own retries.
    if not isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return False
    # The retry is silent and nothing else logs a stalled step.
    logger.warning(
        "[Agent] A model step timed out. It is tried %d times in all before the run fails.",
        STEP_TIMEOUT_RETRIES + 1,
    )
    return True


def retry_timed_out_step(initial_delay: float = 1.0) -> ModelRetryMiddleware:
    """Middleware that retries a model step only when it timed out."""
    return ModelRetryMiddleware(
        max_retries=STEP_TIMEOUT_RETRIES,
        retry_on=_timed_out,
        # "continue" would turn every model error, timeout or not, into a
        # message the agent treats as an answer.
        on_failure="error",
        initial_delay=initial_delay,
    )
