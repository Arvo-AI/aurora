"""Usage tracking helper for orchestrator nodes (triage / synthesis)."""

import logging

from chat.backend.agent.llm import ModelConfig
from chat.backend.agent.utils.llm_usage_tracker import LLMUsageTracker, resolve_serving_provider

logger = logging.getLogger(__name__)


def track_orchestrator_call(state, raw_msg, prompt, request_type: str, start_time: float) -> None:
    """Persist a single triage/synthesis structured-output call to llm_usage_tracking.

    Thin wrapper over `LLMUsageTracker.track_llm_call` that pulls user_id /
    session_id from `State` and resolves model + provider from `ModelConfig`.
    The provider label is the one that actually serves the model (Bedrock for a
    Claude orchestrator under ``LLM_PROVIDER_MODE=bedrock``), not the id prefix,
    and doubles as the pricing mode so Bedrock rows carry the regional premium.
    Safe to call from a background thread (`asyncio.to_thread`).
    """
    if raw_msg is None:
        return
    user_id = getattr(state, "user_id", None)
    if not user_id:
        return
    model_name = ModelConfig.RCA_ORCHESTRATOR_MODEL or ModelConfig.MAIN_MODEL
    api_provider = resolve_serving_provider(model_name) or "unknown"
    try:
        LLMUsageTracker.track_llm_call(
            user_id=user_id,
            session_id=getattr(state, "session_id", None),
            model_name=model_name,
            request_type=request_type,
            prompt=prompt,
            response=raw_msg,
            start_time=start_time,
            api_provider=api_provider,
            provider_mode=api_provider,
        )
    except Exception:
        logger.warning("track_orchestrator_call: failed for %s", request_type, exc_info=True)
