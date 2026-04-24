"""NeMo Guardrails input rail for prompt injection detection.

Runs as a pre-flight check on the user message *before* the LangGraph agent
starts planning, so compromised inputs never reach tool selection. The judge's
streaming path is untouched: NeMo only evaluates the input.

The underlying LLM is the same one the command-safety judge uses
(``GUARDRAILS_LLM_MODEL`` -> ``MAIN_MODEL`` fallback). It is built through the
central ``create_chat_model()`` factory, so provider routing, API keys, and any
future providers are inherited automatically.

Block detection uses NeMo's structured ``triggered_input_rail`` output variable
rather than string-matching refusal text: it is the official signal and is
immune to model-specific wording changes.

Failure policy mirrors the command-safety judge: any unexpected error blocks
the request. Callers can detect this via ``InputRailResult.blocked`` and
``reason``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from langchain_core.language_models import BaseChatModel

from utils.security.command_safety import _fingerprint

logger = logging.getLogger(__name__)

_rails_instance = None

_FAIL_CLOSED_REASON = "input rail unavailable"
_BLOCKED_REASON = "input flagged by safety policy"


@dataclass(frozen=True)
class InputRailResult:
    blocked: bool
    reason: str = ""


def _build_llm() -> BaseChatModel:
    """Build the chat model for the input rail using the shared factory."""
    from chat.backend.agent.llm import ModelConfig
    from chat.backend.agent.providers import create_chat_model
    from utils.security.config import config as gc

    return create_chat_model(
        gc.llm_model or ModelConfig.MAIN_MODEL,
        temperature=0.0,
        streaming=False,
    )


def _get_rails():
    """Lazily build and cache the NeMo LLMRails instance."""
    global _rails_instance
    if _rails_instance is not None:
        return _rails_instance

    import os

    from nemoguardrails import LLMRails, RailsConfig

    config_path = os.path.join(os.path.dirname(__file__), "config")
    rails_config = RailsConfig.from_path(config_path)

    _rails_instance = LLMRails(config=rails_config, llm=_build_llm())
    return _rails_instance


def _triggered_rail_name(result) -> str:
    """Pull the ``triggered_input_rail`` output variable from a NeMo response.

    Returns an empty string when the rail did not fire or the field is absent.
    """
    output_data = getattr(result, "output_data", None)
    if isinstance(output_data, dict):
        name = output_data.get("triggered_input_rail")
        if isinstance(name, str) and name:
            return name
    return ""


async def check_input(user_message: str) -> InputRailResult:
    """Run the NeMo input rail. Returns ``blocked=True`` on unsafe input.

    Fails closed: if the rail itself errors (missing provider creds, model
    unavailable, etc.) the request is blocked with a diagnostic reason.
    """
    from utils.security.config import config

    if not config.enabled:
        return InputRailResult(blocked=False)

    t0 = time.perf_counter()
    try:
        rails = _get_rails()
        result = await rails.generate_async(
            messages=[{"role": "user", "content": user_message}],
            options={
                "rails": ["input"],
                "output_vars": ["triggered_input_rail"],
            },
        )
    except Exception:
        logger.exception("[Guardrails:InputRail] Error running input rail; failing closed")
        return InputRailResult(blocked=True, reason=_FAIL_CLOSED_REASON)

    latency_ms = (time.perf_counter() - t0) * 1000
    triggered = _triggered_rail_name(result)

    if triggered:
        logger.warning(
            "[Guardrails:InputRail] BLOCKED msg_fp=%s msg_len=%d rail=%s latency_ms=%.0f",
            _fingerprint(user_message), len(user_message), triggered, latency_ms,
        )
        return InputRailResult(blocked=True, reason=_BLOCKED_REASON)

    logger.debug("[Guardrails:InputRail] PASSED latency_ms=%.0f", latency_ms)
    return InputRailResult(blocked=False)
