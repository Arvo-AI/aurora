"""NeMo Guardrails input rail for prompt injection detection.

Runs as a pre-flight check on the user message *before* the LangGraph agent
starts planning, so compromised inputs never reach tool selection. The judge's
streaming path is untouched: NeMo only evaluates the input.

The underlying LLM is the same one the command-safety judge uses
(``GUARDRAILS_LLM_MODEL`` -> ``ModelConfig.GUARDRAILS_MODEL``: the cheap tier when
the provider settings can route it, otherwise ``MAIN_MODEL``; a ``MAIN_MODEL``
pinned to a ``bedrock/<id>`` keeps it on that Bedrock geo or on ``MAIN_MODEL``).
It is built through the central ``create_chat_model()`` factory, so provider
routing, API keys, and any future providers are inherited automatically.

Block detection uses NeMo's structured ``triggered_input_rail`` output variable
rather than string-matching refusal text: it is the official signal and is
immune to model-specific wording changes.

Failure policy mirrors the command-safety judge: any unexpected error blocks
the request. Callers can detect this via ``InputRailResult.blocked`` and
``reason``.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional, Tuple

from langchain_core.language_models import BaseChatModel

from chat.backend.agent.providers._sampling_guard import SAMPLING_FIELDS
from utils import usage_writer
from utils.security.command_safety import _fingerprint

logger = logging.getLogger(__name__)

_rails_instance = None
_rails_lock: asyncio.Lock | None = None
_rails_thread_lock = threading.Lock()
_last_init_failure_ts: float = float("-inf")
_INIT_FAILURE_BACKOFF_S = 30.0

# (user_id, session_id) of the request whose message the rail is screening. NeMo owns
# the model call, so ``check_input`` publishes the ids here and the LLM wrapper reads
# them when it records usage; ``None`` (no ids known) means "do not record".
_usage_context: contextvars.ContextVar[Optional[Tuple[str, Optional[str]]]] = contextvars.ContextVar(
    "input_rail_usage_context", default=None
)
_USAGE_REQUEST_TYPE = "input_rail"

_FAIL_CLOSED_REASON = "input rail unavailable"
_FAIL_CLOSED_AUTH = "input rail auth error"
_FAIL_CLOSED_CONNECTIVITY = "input rail connectivity error"
_BLOCKED_REASON = "input flagged by safety policy"


def _first_message(result):
    """The first message of a ``ChatResult`` / ``LLMResult``, or ``None``.

    ``ChatResult.generations`` is a flat list of ChatGeneration; an ``LLMResult``
    nests them one list per prompt. Both shapes are accepted.
    """
    for gen in getattr(result, "generations", None) or []:
        candidates = gen if isinstance(gen, (list, tuple)) else [gen]
        for candidate in candidates:
            msg = getattr(candidate, "message", None)
            if msg is not None:
                return msg
    return None


def _get_lock() -> asyncio.Lock:
    """Create the init lock lazily so it binds to the active event loop."""
    global _rails_lock
    if _rails_lock is None:
        _rails_lock = asyncio.Lock()
    return _rails_lock


def _record_init_failure() -> None:
    """Stamp failure time so concurrent callers back off briefly."""
    global _last_init_failure_ts
    _last_init_failure_ts = time.monotonic()


@dataclass(frozen=True)
class InputRailResult:
    blocked: bool
    reason: str = ""
    latency_ms: float = 0.0


class _GuardrailsLLMCompat(BaseChatModel):
    """Adapt chat models for NeMo Guardrails self-check tasks.

    Three incompatibilities are bridged here:

    1. NeMo calls ``llm.bind(temperature=..., max_tokens=...)`` at invoke time
       (``LangChainLLMAdapter.generate_async`` in
       ``nemoguardrails/integrations/langchain/llm_adapter.py``). Gemini's
       ``GenerateContentConfig`` only knows ``max_output_tokens`` and rejects
       the alias as ``extra_forbidden``. We rename the key at bind time when
       ``rename_max_tokens`` is set (Gemini direct path only).
    2. Newer models removed sampling parameters entirely — Claude Opus 5.5 /
       Sonnet 5 answer ``400 `temperature` is deprecated for this model`` and
       GPT-6 Astra ``400 Unsupported parameter: 'temperature'``. Because NeMo
       re-attaches ``temperature`` as a *call* kwarg on every self-check, the
       providers' ``_sampling_guard`` (which only clears instance attributes)
       cannot rescue it and the rail fails closed, blocking every request. We
       drop the rejected params from the call kwargs and retry once, then keep
       them off for the life of the instance.
    3. Reasoning models (Gemini ``gemini-2.5+``, OpenAI ``gpt-5+`` via the
       Responses API, Claude extended thinking) return structured content (a
       list of ``{"type": "reasoning" | "thinking" | "text", ...}`` blocks)
       while NeMo calls ``.strip()`` on the returned string. We flatten list
       content to its text blocks so the ``str`` contract NeMo expects holds.

    The wrapper is installed for every provider: (2) and (3) are model-specific,
    not provider-specific, and a model that accepts the params is unaffected
    because nothing is stripped until it complains.
    """

    inner: BaseChatModel
    rename_max_tokens: bool = False
    # Flipped to True the first time the model rejects a sampling param, so later
    # calls skip the wasted round-trip. Not a constructor argument in practice.
    drop_sampling: bool = False
    # Labels for the llm_usage_tracking rows: the Aurora model id the rail runs on
    # and the provider that actually serves it. Empty = usage is not recorded.
    usage_model_name: str = ""
    usage_provider: str = ""

    model_config = {"arbitrary_types_allowed": True}

    @property
    def _llm_type(self) -> str:
        return self.inner._llm_type

    def _record_usage(self, messages, result, start_time: float) -> None:
        """Store one ``input_rail`` usage row for a finished self-check call.

        Reads the request ids ``check_input`` published in ``_usage_context``; a
        rail built outside a request (no ids) records nothing. The provider's
        ``usage_metadata`` on the returned message carries the real token counts.
        Never raises — tracking can never fail the rail.
        """
        try:
            ids = _usage_context.get()
            if not ids or not self.usage_model_name:
                return
            user_id, session_id = ids
            if not user_id:
                return

            from chat.backend.agent.utils.llm_usage_tracker import LLMUsageTracker

            LLMUsageTracker.track_llm_call(
                user_id=user_id,
                session_id=session_id,
                model_name=self.usage_model_name,
                request_type=_USAGE_REQUEST_TYPE,
                prompt=messages,
                response=_first_message(result),
                start_time=start_time,
                api_provider=self.usage_provider or "direct",
                provider_mode=self.usage_provider or None,
            )
        except Exception:  # noqa: BLE001
            logger.debug("[Guardrails:InputRail] Could not record rail usage", exc_info=True)

    def _rename(self, kwargs: dict) -> dict:
        if self.rename_max_tokens and "max_tokens" in kwargs and "max_output_tokens" not in kwargs:
            kwargs = dict(kwargs)
            kwargs["max_output_tokens"] = kwargs.pop("max_tokens")
        return kwargs

    def _prepare(self, kwargs: dict) -> dict:
        """Apply the Gemini rename, then drop sampling params the model has rejected."""
        prepared = self._rename(kwargs)
        if self.drop_sampling and any(f in prepared for f in SAMPLING_FIELDS):
            prepared = {k: v for k, v in prepared.items() if k not in SAMPLING_FIELDS}
        return prepared

    def _note_sampling_rejection(self, err: Exception, kwargs: dict) -> bool:
        """Return True when ``err`` is a sampling-param rejection we can retry around.

        False for anything else (or when the params are already stripped, so a retry
        would be identical) — the caller then re-raises and the rail fails closed.
        """
        if self.drop_sampling:
            return False
        if not any(field in str(err).lower() for field in SAMPLING_FIELDS):
            return False
        if not any(field in self._rename(kwargs) for field in SAMPLING_FIELDS):
            return False
        self.drop_sampling = True
        logger.warning(
            "[Guardrails:InputRail] Rail model rejected a sampling parameter; "
            "retrying without it and dropping it for subsequent checks."
        )
        return True

    @staticmethod
    def _flatten(result):
        """Collapse reasoning-block lists to plain text on each message.

        Handles Gemini ``thinking`` blocks and OpenAI Responses-API
        ``reasoning`` blocks alongside regular ``text`` blocks; only
        ``text`` content is preserved so NeMo's ``.strip()`` succeeds.
        """
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
        # Return a RunnableBinding that wraps ``self`` (not ``self.inner``) so the
        # rename / sampling-strip / flatten handling still runs at invoke time.
        from langchain_core.runnables import RunnableBinding
        return RunnableBinding(bound=self, kwargs=kwargs)

    def _generate(self, messages, stop=None, **kwargs):
        start_time = time.time()
        try:
            result = self._flatten(
                self.inner._generate(messages, stop=stop, **self._prepare(kwargs))
            )
        except Exception as err:  # noqa: BLE001
            # Model removed temperature/top_p/top_k — drop them and retry once.
            if not self._note_sampling_rejection(err, kwargs):
                raise
            result = self._flatten(
                self.inner._generate(messages, stop=stop, **self._prepare(kwargs))
            )
        self._record_usage(messages, result, start_time)
        return result

    async def _agenerate(self, messages, stop=None, **kwargs):
        start_time = time.time()
        try:
            result = self._flatten(
                await self.inner._agenerate(messages, stop=stop, **self._prepare(kwargs))
            )
        except Exception as err:  # noqa: BLE001
            # Model removed temperature/top_p/top_k — drop them and retry once.
            if not self._note_sampling_rejection(err, kwargs):
                raise
            result = self._flatten(
                await self.inner._agenerate(messages, stop=stop, **self._prepare(kwargs))
            )
        # The usage row is a blocking DB write and tracking is best-effort: hand it to
        # the process-owned usage writer thread and do not wait for it, so the rail
        # verdict (and the main model call behind it) is never delayed by the database.
        # The writer runs the call under a snapshot of the current context, so it still
        # sees the ids ``check_input`` published even though ``check_input`` resets them
        # right after. Unlike an asyncio task, the write survives ``asyncio.run()``
        # tearing the loop down at the end of a background RCA; that run drains the
        # writer before it returns (see ``chat.background.task``).
        if _usage_context.get() and self.usage_model_name:
            usage_writer.submit(self._record_usage, messages, result, start_time)
        return result


def _build_llm() -> BaseChatModel:
    """Build the chat model for the input rail using the shared factory.

    Always wrapped in :class:`_GuardrailsLLMCompat`: the wrapper handles two
    model-specific (not provider-specific) hazards — newer models that reject
    ``temperature`` (which NeMo re-attaches on every self-check call) and
    reasoning models whose structured content breaks NeMo's ``str.strip()``.
    Neither costs anything on a model that doesn't hit them. Gemini direct
    additionally needs the ``max_tokens`` kwarg renamed.
    """
    import os

    from chat.backend.agent.llm import ModelConfig
    from chat.backend.agent.providers import create_chat_model
    from utils.security.config import config as gc

    model_name = gc.llm_model or ModelConfig.GUARDRAILS_MODEL
    # The rail is a classifier: extended thinking off where the model allows it.
    llm = create_chat_model(
        model_name, temperature=0.0, streaming=False, disable_thinking=True
    )

    provider_mode = os.getenv("LLM_PROVIDER_MODE", "direct").lower()

    from chat.backend.agent.providers import get_registry
    serving_provider = get_registry().resolve_provider_name(model_name, mode=provider_mode)

    # Gemini's GenerateContentConfig rejects the `max_tokens` alias NeMo sends.
    # The usage labels make every self-check call show up in llm_usage_tracking
    # (request_type "input_rail"), billed to the provider that serves the model.
    return _GuardrailsLLMCompat(
        inner=llm,
        rename_max_tokens=serving_provider in ("google", "vertex"),
        usage_model_name=model_name,
        usage_provider=serving_provider or provider_mode,
    )


def _build_rails_sync():
    """Blocking NeMo rails construction. Runs off the event loop via to_thread."""
    import os

    from nemoguardrails import LLMRails, RailsConfig

    config_path = os.path.join(os.path.dirname(__file__), "config")
    rails_config = RailsConfig.from_path(config_path)
    return LLMRails(config=rails_config, llm=_build_llm())


def _ensure_rails_in_thread():
    """Build or return cached rails under ``_rails_thread_lock``.

    Runs in a worker thread (``asyncio.to_thread`` or Celery prewarm) so the
    event loop never blocks on ``threading.Lock``. Backoff is checked here, not
    only before lock acquisition, so a caller cannot pass an outer check, wait
    on the lock, and rebuild after a concurrent failure stamped backoff.
    """
    global _rails_instance
    with _rails_thread_lock:
        if _rails_instance is not None:
            return _rails_instance
        if time.monotonic() - _last_init_failure_ts < _INIT_FAILURE_BACKOFF_S:
            raise RuntimeError("input rail init recently failed; backing off")
        try:
            _rails_instance = _build_rails_sync()
        except Exception:
            _record_init_failure()
            raise
    return _rails_instance


async def _get_rails():
    """Lazily build and cache the NeMo LLMRails instance.

    Construction is synchronous (YAML parse + model build) so we run it off
    the event loop. Failures are negative-cached for a short window so a
    flapping provider does not block the loop on every request.
    """
    global _rails_instance
    if _rails_instance is not None:
        return _rails_instance

    # Fast-fail without spawning a worker thread when backoff is active.
    if time.monotonic() - _last_init_failure_ts < _INIT_FAILURE_BACKOFF_S:
        raise RuntimeError("input rail init recently failed; backing off")

    async with _get_lock():
        if _rails_instance is not None:
            return _rails_instance
        if time.monotonic() - _last_init_failure_ts < _INIT_FAILURE_BACKOFF_S:
            raise RuntimeError("input rail init recently failed; backing off")
        return await asyncio.to_thread(_ensure_rails_in_thread)


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


def _classify_failure(exc: Exception) -> str:
    """Bucket a rail failure into auth / connectivity / generic."""
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status in (401, 403):
        return _FAIL_CLOSED_AUTH
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return _FAIL_CLOSED_CONNECTIVITY
    # SDK-specific types that don't subclass the builtins
    for parent in type(exc).__mro__:
        name = parent.__qualname__
        if "Auth" in name or "Permission" in name or "Forbidden" in name:
            return _FAIL_CLOSED_AUTH
        if "Connect" in name or "Timeout" in name:
            return _FAIL_CLOSED_CONNECTIVITY
    return _FAIL_CLOSED_REASON


async def check_input(
    user_message: str,
    *,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> InputRailResult:
    """Run the NeMo input rail. Returns ``blocked=True`` on unsafe input.

    Fails closed: if the rail itself errors (missing provider creds, model
    unavailable, etc.) the request is blocked with a diagnostic reason.

    ``user_id`` / ``session_id`` label the ``llm_usage_tracking`` rows of the
    self-check calls NeMo makes on this message; without a ``user_id`` nothing
    is recorded (the rail itself behaves identically either way).
    """
    from utils.security.config import config

    if not config.enabled:
        return InputRailResult(blocked=False)

    t0 = time.perf_counter()
    usage_token = _usage_context.set((user_id, session_id) if user_id else None)
    try:
        rails = await _get_rails()
        result = await rails.generate_async(
            messages=[{"role": "user", "content": user_message}],
            options={
                "rails": ["input"],
                "output_vars": ["triggered_input_rail"],
            },
        )
    except Exception as exc:
        latency_ms = (time.perf_counter() - t0) * 1000
        logger.exception("[Guardrails:InputRail] Error running input rail; failing closed")
        reason = _classify_failure(exc)
        return InputRailResult(blocked=True, reason=reason, latency_ms=latency_ms)
    finally:
        _usage_context.reset(usage_token)

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
