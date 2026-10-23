"""Retry once without cache markers when native Bedrock rejects them.

A new ``ChatBedrockConverse`` is built every turn, so remembering the rejection
on the instance would pay for a failed call each turn. The model id and region
are remembered for the process instead. Wrapped only around
``ChatBedrockConverse``. Mirrors ``_sampling_guard``.
"""

import logging

from pydantic import PrivateAttr

from chat.backend.agent.utils.cache_control import (
    strip_bedrock_tool_cache_points,
    strip_cache_markers,
)

logger = logging.getLogger(__name__)

_REJECTION_HINTS = ("cachepoint", "cache point", "cache_point", "prompt caching", "cache_control")

_cache_safe_classes: dict = {}
# "region:model_id" pairs Bedrock has already rejected cache markers for.
_rejected_models: set[str] = set()


def _rejection_key(model) -> str:
    """Model id plus region, so one region's rejection does not disable another."""
    model_id = getattr(model, "model_id", None)
    # No id (tests, unexpected client): remember on the instance only
    if not isinstance(model_id, str) or not model_id:
        return ""
    region = getattr(model, "region_name", None)
    if not isinstance(region, str):
        region = ""
    return f"{region}:{model_id}"


def is_bedrock_cache_rejection(err: BaseException) -> bool:
    """True when ``err`` complains about a Bedrock prompt-cache marker."""
    msg = str(err).lower()
    return any(hint in msg for hint in _REJECTION_HINTS)


def make_bedrock_cache_safe_cls(base_cls):
    """Return (and cache) a subclass of ``base_cls`` that self-heals around Bedrock cache markers.

    Wraps the sync ``_generate`` / ``_stream`` paths only, matching
    ``ChatBedrockConverse``, whose async paths dispatch to the sync ones. The stream
    wrapper retries only if nothing was yielded, so a retry never double-emits.
    """
    cached = _cache_safe_classes.get(base_cls)
    if cached is not None:
        return cached

    class _CacheSafe(base_cls):
        _cache_markers_disabled: bool = PrivateAttr(default=False)

        def _markers_known_bad(self) -> bool:
            if self._cache_markers_disabled:
                return True
            key = _rejection_key(self)
            return bool(key) and key in _rejected_models

        def _disable_markers(self, err) -> bool:
            # Already known bad, or the error isn't about caching: nothing new to retry with
            if self._markers_known_bad() or not is_bedrock_cache_rejection(err):
                return False
            self._cache_markers_disabled = True
            key = _rejection_key(self)
            # Remembered for every later instance of this model in this region
            if key:
                _rejected_models.add(key)
            logger.warning("Bedrock rejected a prompt-cache marker; retrying without caching.")
            return True

        def _generate(self, messages, *args, **kwargs):
            # This model already rejected markers: send clean from the start
            if self._markers_known_bad():
                messages, kwargs = strip_cache_markers(messages), strip_bedrock_tool_cache_points(kwargs)
            try:
                return super()._generate(messages, *args, **kwargs)
            except Exception as err:  # noqa: BLE001
                if not self._disable_markers(err):
                    raise
                return super()._generate(
                    strip_cache_markers(messages), *args, **strip_bedrock_tool_cache_points(kwargs)
                )

        def _stream(self, messages, *args, **kwargs):
            # This model already rejected markers: send clean from the start
            if self._markers_known_bad():
                messages, kwargs = strip_cache_markers(messages), strip_bedrock_tool_cache_points(kwargs)
            yielded = False
            try:
                for chunk in super()._stream(messages, *args, **kwargs):
                    yielded = True
                    yield chunk
            except Exception as err:  # noqa: BLE001
                if yielded or not self._disable_markers(err):
                    raise
                yield from super()._stream(
                    strip_cache_markers(messages), *args, **strip_bedrock_tool_cache_points(kwargs)
                )

    _CacheSafe.__name__ = f"_CacheSafe{base_cls.__name__}"
    _CacheSafe.__qualname__ = _CacheSafe.__name__
    _cache_safe_classes[base_cls] = _CacheSafe
    return _CacheSafe
