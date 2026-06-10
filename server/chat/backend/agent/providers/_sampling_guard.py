"""
Shared sampling-parameter guard for chat-model providers.

Some Claude models (e.g. Anthropic Opus 4.7+) removed ``temperature`` / ``top_p`` /
``top_k`` and return a 400 when those params are sent — on the direct Anthropic API
(``"temperature is deprecated for this model"``) and on Bedrock Converse (a
``ValidationException`` naming the field). Aurora hardcodes ``temperature=0.4``, so any
call to such a model fails unless the param is dropped.

Rather than hardcode a per-model list (which would need updating for every future
model), we react to the model's own error: on a rejection whose message names a
sampling field, drop the offending params and retry once. :func:`make_adaptive_sampling_cls`
builds the thin adaptive subclass used by the Anthropic (``ChatAnthropic``), Bedrock-native
(``ChatBedrockConverse``), and Bedrock-gateway (OpenAI-compatible ``ChatOpenAI``) clients.
"""

import logging

logger = logging.getLogger(__name__)

# Sampling params some models accept and newer ones reject outright.
SAMPLING_FIELDS = ("temperature", "top_p", "top_k")


def _model_label(model) -> str:
    """Best-effort model identifier across client classes. Order matches
    ``llm.py``'s ``_model_label``: prefer ``model_name``, then ``model_id`` (the
    canonical attr on ChatBedrockConverse — ``.model`` there is only an input
    alias), then ``model``."""
    return (
        getattr(model, "model_name", None)
        or getattr(model, "model_id", None)
        or getattr(model, "model", None)
        or type(model).__name__
    )


def strip_rejected_sampling(model, err, logger) -> bool:
    """If ``err`` names a sampling param, clear the ones currently set on ``model``.

    Returns True only when something was actually stripped — so a retry differs from
    the failed call, and a second already-stripped call returns False (no retry loop).
    Errors that don't name a sampling field return False and should be re-raised.
    """
    msg = str(err).lower()
    if not any(field in msg for field in SAMPLING_FIELDS):
        return False
    stripped = False
    for field in SAMPLING_FIELDS:
        if getattr(model, field, None) is not None:
            setattr(model, field, None)
            stripped = True
    if stripped:
        logger.warning(
            "Model %s rejected a sampling parameter; retrying without it.",
            _model_label(model),
        )
    return stripped


_adaptive_cache: dict = {}


def make_adaptive_sampling_cls(base_cls, *, wrap_async: bool = True):
    """Return (and cache) a subclass of ``base_cls`` that self-heals around rejected
    sampling params, reusing :func:`strip_rejected_sampling`.

    On an error naming a sampling field, the subclass strips it and retries once, then
    keeps it off for the life of the instance. Wraps the sync paths (``_generate`` /
    ``_stream``) always, and the async paths (``_agenerate`` / ``_astream``) when
    ``wrap_async`` is True. Pass ``wrap_async=False`` for a client whose async path
    dispatches to the sync one via ``BaseChatModel`` (e.g. ``ChatBedrockConverse``) —
    wrapping async there would retry twice. The deprecation 400 is raised before any
    tokens stream, and the streaming wrappers only retry when nothing has been yielded,
    so a retry never double-emits.
    """
    cache_key = (base_cls, wrap_async)
    cached = _adaptive_cache.get(cache_key)
    if cached is not None:
        return cached

    class _Adaptive(base_cls):
        """Drops sampling params a model rejects, then remembers (instance cached)."""

        def _generate(self, *args, **kwargs):
            try:
                return super()._generate(*args, **kwargs)
            except Exception as err:  # noqa: BLE001
                if not strip_rejected_sampling(self, err, logger):
                    raise
                return super()._generate(*args, **kwargs)

        def _stream(self, *args, **kwargs):
            yielded = False
            try:
                for chunk in super()._stream(*args, **kwargs):
                    yielded = True
                    yield chunk
            except Exception as err:  # noqa: BLE001
                if yielded or not strip_rejected_sampling(self, err, logger):
                    raise
                yield from super()._stream(*args, **kwargs)

        if wrap_async:

            async def _agenerate(self, *args, **kwargs):
                try:
                    return await super()._agenerate(*args, **kwargs)
                except Exception as err:  # noqa: BLE001
                    if not strip_rejected_sampling(self, err, logger):
                        raise
                    return await super()._agenerate(*args, **kwargs)

            async def _astream(self, *args, **kwargs):
                yielded = False
                try:
                    async for chunk in super()._astream(*args, **kwargs):
                        yielded = True
                        yield chunk
                except Exception as err:  # noqa: BLE001
                    if yielded or not strip_rejected_sampling(self, err, logger):
                        raise
                    async for chunk in super()._astream(*args, **kwargs):
                        yield chunk

    _Adaptive.__name__ = f"_Adaptive{base_cls.__name__}"
    _Adaptive.__qualname__ = _Adaptive.__name__
    _adaptive_cache[cache_key] = _Adaptive
    return _Adaptive
