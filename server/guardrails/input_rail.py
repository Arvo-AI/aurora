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

import asyncio
import logging
import time
from dataclasses import dataclass

from langchain_core.language_models import BaseChatModel

from utils.security.command_safety import _fingerprint

logger = logging.getLogger(__name__)

_rails_instance = None
_rails_lock: asyncio.Lock | None = None
_last_init_failure_ts: float = 0.0
_INIT_FAILURE_BACKOFF_S = 30.0

_FAIL_CLOSED_REASON = "input rail unavailable"
_BLOCKED_REASON = "input flagged by safety policy"


def _get_lock() -> asyncio.Lock:
    """Create the init lock lazily so it binds to the active event loop."""
    global _rails_lock
    if _rails_lock is None:
        _rails_lock = asyncio.Lock()
    return _rails_lock


@dataclass(frozen=True)
class InputRailResult:
    blocked: bool
    reason: str = ""
    latency_ms: float = 0.0


class _GeminiMaxTokensCompat(BaseChatModel):
    """Adapt ``ChatGoogleGenerativeAI`` for NeMo Guardrails self-check tasks.

    Two incompatibilities are bridged here:

    1. NeMo calls ``llm.bind(max_tokens=3, ...)`` at invoke time
       (``nemoguardrails/library/self_check/input_check/actions.py``). Gemini's
       ``GenerateContentConfig`` only knows ``max_output_tokens`` and rejects
       the alias as ``extra_forbidden``. We rename the key at bind time.
    2. ``gemini-2.5`` and newer return structured content (a list of
       ``{"type": "thinking" | "text", ...}`` blocks) while NeMo calls
       ``.strip()`` on the returned string. We flatten list content to its
       text blocks so the ``str`` contract NeMo expects holds.

    The wrapper is only installed when the guardrails model is Gemini routed
    via the direct Google provider, so every other provider is untouched.
    """

    inner: BaseChatModel

    model_config = {"arbitrary_types_allowed": True}

    @property
    def _llm_type(self) -> str:
        return self.inner._llm_type

    @staticmethod
    def _rename(kwargs: dict) -> dict:
        if "max_tokens" in kwargs and "max_output_tokens" not in kwargs:
            kwargs = dict(kwargs)
            kwargs["max_output_tokens"] = kwargs.pop("max_tokens")
        return kwargs

    @staticmethod
    def _flatten(result):
        """Collapse Gemini thinking-block lists to plain text on each message."""
        from langchain_core.messages import BaseMessage
        for gen in getattr(result, "generations", []) or []:
            msg = getattr(gen, "message", None)
            if not isinstance(msg, BaseMessage):
                continue
            content = msg.content
            if isinstance(content, list):
                parts = [
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                msg.content = "".join(parts)
        return result

    def bind(self, **kwargs):
        # Return a RunnableBinding that wraps ``self`` (not ``self.inner``) so
        # the flatten post-processing still runs at invoke time.
        from langchain_core.runnables import RunnableBinding
        return RunnableBinding(bound=self, kwargs=self._rename(kwargs))

    def _generate(self, messages, stop=None, **kwargs):
        return self._flatten(
            self.inner._generate(messages, stop=stop, **self._rename(kwargs))
        )

    async def _agenerate(self, messages, stop=None, **kwargs):
        return self._flatten(
            await self.inner._agenerate(messages, stop=stop, **self._rename(kwargs))
        )


def _build_llm() -> BaseChatModel:
    """Build the chat model for the input rail using the shared factory.

    When guardrails are served by Gemini via the direct Google provider, wrap
    the model so NeMo's ``max_tokens`` bind kwarg is translated to the name
    google-genai expects.
    """
    import os

    from chat.backend.agent.llm import ModelConfig
    from chat.backend.agent.model_mapper import ModelMapper
    from chat.backend.agent.providers import create_chat_model
    from utils.security.config import config as gc

    model_name = gc.llm_model or ModelConfig.MAIN_MODEL
    llm = create_chat_model(model_name, temperature=0.0, streaming=False)

    provider, _ = ModelMapper.split_provider_model(model_name)
    provider_mode = os.getenv("LLM_PROVIDER_MODE", "direct").lower()
    if provider == "google" and provider_mode == "direct":
        return _GeminiMaxTokensCompat(inner=llm)
    return llm


def _build_rails_sync():
    """Blocking NeMo rails construction. Runs off the event loop via to_thread."""
    import os

    from nemoguardrails import LLMRails, RailsConfig

    config_path = os.path.join(os.path.dirname(__file__), "config")
    rails_config = RailsConfig.from_path(config_path)
    return LLMRails(config=rails_config, llm=_build_llm())


async def _get_rails():
    """Lazily build and cache the NeMo LLMRails instance.

    Construction is synchronous (YAML parse + model build) so we run it off
    the event loop. Failures are negative-cached for a short window so a
    flapping provider does not block the loop on every request.
    """
    global _rails_instance, _last_init_failure_ts
    if _rails_instance is not None:
        return _rails_instance

    if time.monotonic() - _last_init_failure_ts < _INIT_FAILURE_BACKOFF_S:
        raise RuntimeError("input rail init recently failed; backing off")

    async with _get_lock():
        if _rails_instance is not None:
            return _rails_instance
        try:
            _rails_instance = await asyncio.to_thread(_build_rails_sync)
        except Exception:
            _last_init_failure_ts = time.monotonic()
            raise
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
        rails = await _get_rails()
        result = await rails.generate_async(
            messages=[{"role": "user", "content": user_message}],
            options={
                "rails": ["input"],
                "output_vars": ["triggered_input_rail"],
            },
        )
    except Exception:
        latency_ms = (time.perf_counter() - t0) * 1000
        logger.exception("[Guardrails:InputRail] Error running input rail; failing closed")
        return InputRailResult(blocked=True, reason=_FAIL_CLOSED_REASON, latency_ms=latency_ms)

    latency_ms = (time.perf_counter() - t0) * 1000
    triggered = _triggered_rail_name(result)

    if triggered:
        logger.warning(
            "[Guardrails:InputRail] BLOCKED msg_fp=%s msg_len=%d rail=%s latency_ms=%.0f",
            _fingerprint(user_message), len(user_message), triggered, latency_ms,
        )
        return InputRailResult(blocked=True, reason=_BLOCKED_REASON, latency_ms=latency_ms)

    logger.debug("[Guardrails:InputRail] PASSED latency_ms=%.0f", latency_ms)
    return InputRailResult(blocked=False, latency_ms=latency_ms)
