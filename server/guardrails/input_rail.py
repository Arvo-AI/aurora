"""NeMo Guardrails input rail for prompt injection detection.

Uses LLMRails.generate_async() as a pre-flight check on user messages
*before* the LangGraph agent runs. This preserves streaming -- NeMo only
gates the input; the existing agent handles generation untouched.

Model and credentials are resolved from the same config the LLM safety
judge uses (GUARDRAILS_LLM_MODEL -> MAIN_MODEL fallback, with provider
auto-detection from LLM_PROVIDER_MODE).
"""

import logging
import os
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_rails_instance = None

_REFUSAL_PREFIXES = ("i'm sorry", "i am sorry", "i cannot", "i can't")


@dataclass(frozen=True)
class InputRailResult:
    blocked: bool
    reason: str = ""


def _extract_response_text(result) -> str:
    """Pull the assistant response text out of a NeMo GenerationResponse."""
    resp = getattr(result, "response", None)
    if isinstance(resp, list) and resp and isinstance(resp[0], dict):
        return resp[0].get("content", "") or ""
    if isinstance(result, dict):
        return result.get("content", "") or ""
    return ""


def _resolve_model_and_credentials() -> tuple:
    """Resolve (model_name, base_url, api_key) using the same logic as the LLM judge."""
    from utils.security.config import config as gc

    model_name = gc.llm_model
    if not model_name:
        from chat.backend.agent.llm import ModelConfig
        model_name = ModelConfig.MAIN_MODEL

    if gc.llm_base_url:
        return model_name, gc.llm_base_url, gc.llm_api_key or ""

    provider = os.getenv("LLM_PROVIDER_MODE", "").lower()
    if provider == "openrouter":
        return model_name, "https://openrouter.ai/api/v1", os.getenv("OPENROUTER_API_KEY", "")
    if provider == "google":
        return model_name, "https://generativelanguage.googleapis.com/v1beta/openai", os.getenv("GOOGLE_AI_API_KEY", "")

    return model_name, "", os.getenv("OPENAI_API_KEY", "")


def _get_rails():
    """Lazily create and cache the NeMo LLMRails instance."""
    global _rails_instance
    if _rails_instance is not None:
        return _rails_instance

    model_name, base_url, api_key = _resolve_model_and_credentials()
    if not api_key:
        raise RuntimeError("No API key available for NeMo input rail LLM")

    from nemoguardrails import LLMRails, RailsConfig
    from nemoguardrails.rails.llm.config import Model

    config_path = os.path.join(os.path.dirname(__file__), "config")
    config = RailsConfig.from_path(config_path)

    params = {"openai_api_key": api_key}
    if base_url:
        params["openai_api_base"] = base_url

    config.models = [Model(type="main", engine="openai", model=model_name, parameters=params)]

    _rails_instance = LLMRails(config)
    return _rails_instance


async def check_input(user_message: str) -> InputRailResult:
    """Run NeMo input rail on a user message. Returns blocked=True if unsafe."""
    from utils.security.config import config

    if not config.enabled or not config.input_rail:
        return InputRailResult(blocked=False)

    t0 = time.perf_counter()
    try:
        rails = _get_rails()
        result = await rails.generate_async(
            messages=[{"role": "user", "content": user_message}],
            options={"rails": ["input"]},
        )

        latency_ms = (time.perf_counter() - t0) * 1000
        response_text = _extract_response_text(result)
        lowered = response_text.lower().lstrip()
        blocked = lowered.startswith(_REFUSAL_PREFIXES)

        if blocked:
            logger.warning(
                "[Guardrails:InputRail] BLOCKED user_message=%s latency_ms=%.0f",
                user_message[:80], latency_ms,
            )
        else:
            logger.debug("[Guardrails:InputRail] PASSED latency_ms=%.0f", latency_ms)

        return InputRailResult(blocked=blocked, reason=response_text if blocked else "")

    except Exception:
        logger.exception("[Guardrails:InputRail] Error running input rail, failing open")
        return InputRailResult(blocked=False)
