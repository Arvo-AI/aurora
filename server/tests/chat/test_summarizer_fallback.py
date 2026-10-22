"""Shared summarizer falls back to the auxiliary default model when the configured
summarization model has no provider in this deployment (Aurora Risk Review on #740).

Scenario: a deployment that still carries ``SUMMARIZATION_MODEL=google/gemini-2.5-flash``
from an older install runs with ``LLM_PROVIDER_MODE=bedrock`` and no Google credentials.
``TOOL_OUTPUT_SUMMARIZATION_MODEL`` inherits that value, so before this fix every tool
output above the pass-through threshold failed in ``summarize_tool_output_once`` and
``cap_tool_output`` hard-truncated it ("[Output truncated — summarization failed]").
Now ``summarize_text`` retries once on ``ModelConfig.AUXILIARY_DEFAULT_MODEL`` (routable
by construction) and only for the typed ``ProviderUnavailableError`` — a configuration
fact — never for network or model errors.
"""
import importlib
import importlib.util
import logging
from types import SimpleNamespace

import pytest

from chat.backend.agent.call_options import CHEAP_MODEL
from chat.backend.agent.providers import ProviderRegistry, ProviderUnavailableError
from chat.backend.agent.utils import tool_output_summary as tos

_UNSERVABLE = "google/gemini-2.5-flash"
_SCENARIO_ENV = {
    "SUMMARIZATION_MODEL": _UNSERVABLE,
    "LLM_PROVIDER_MODE": "bedrock",
    "BEDROCK_REGION": "eu-central-1",
}
_SCRUBBED = (
    "TOOL_OUTPUT_SUMMARIZATION_MODEL", "MAIN_MODEL", "GOOGLE_AI_API_KEY", "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY", "OPENROUTER_API_KEY", "BEDROCK_BASE_URL", "AWS_REGION", "AWS_DEFAULT_REGION",
    "LLM_MAX_TOKENS_SUMMARY",
)


def _apply_scenario(monkeypatch):
    for var in _SCRUBBED:
        monkeypatch.delenv(var, raising=False)
    for var, value in _SCENARIO_ENV.items():
        monkeypatch.setenv(var, value)


def _fresh_llm_module():
    """``chat.backend.agent.llm`` evaluated under the current env (ModelConfig is
    computed at import). Throwaway module object; never enters ``sys.modules``."""
    real = importlib.import_module("chat.backend.agent.llm")
    spec = importlib.util.spec_from_file_location("_fresh_llm_for_fallback", real.__file__)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    return fresh


class _FakeSummarizer:
    def __init__(self, model):
        self.model = model

    def invoke(self, prompt):
        return SimpleNamespace(content=f"SUMMARY by {self.model}")


def _fake_factory(unavailable=(_UNSERVABLE,), error=None):
    """Stand-in for ``get_cached_chat_model`` that mirrors the registry's behaviour:
    ``ProviderUnavailableError`` for models in ``unavailable`` (or ``error`` when given),
    a working fake summarizer for everything else. Records every model asked for."""
    calls = []

    def _factory(model, temperature=0.0, provider_mode=None, **kwargs):
        calls.append(model)
        if model in unavailable:
            raise error or ProviderUnavailableError(
                f"Provider 'google' is not available for model '{model}'. Configure GOOGLE_AI_API_KEY"
            )
        return _FakeSummarizer(model)

    _factory.calls = calls
    return _factory


def test_registry_raises_the_typed_error_in_the_scenario(monkeypatch):
    """The real routing: forced bedrock cannot serve ``google/``, prefix routing lands
    on the Google provider, which has no key -> ProviderUnavailableError (still a
    RuntimeError for existing ``except RuntimeError`` callers)."""
    _apply_scenario(monkeypatch)
    registry = ProviderRegistry()
    with pytest.raises(ProviderUnavailableError, match="GOOGLE_AI_API_KEY") as info:
        registry.get_provider_for_model(_UNSERVABLE, mode="bedrock")
    assert isinstance(info.value, RuntimeError)
    # The fallback model, by contrast, is served by Bedrock in this deployment.
    assert registry.resolve_provider_name(CHEAP_MODEL, "bedrock") == "bedrock"


def test_scenario_resolves_tool_output_model_to_the_unservable_gemini(monkeypatch):
    _apply_scenario(monkeypatch)
    cfg = _fresh_llm_module().ModelConfig
    assert cfg.TOOL_OUTPUT_SUMMARIZATION_MODEL == _UNSERVABLE, "inherits SUMMARIZATION_MODEL"
    assert cfg.AUXILIARY_DEFAULT_MODEL == CHEAP_MODEL, "routable through Bedrock in the EU"


def test_tool_output_is_summarized_by_the_fallback_instead_of_truncated(monkeypatch, caplog):
    _apply_scenario(monkeypatch)
    llm = _fresh_llm_module()
    factory = _fake_factory()
    monkeypatch.setattr(llm, "get_cached_chat_model", factory)

    with caplog.at_level(logging.WARNING, logger=llm.__name__):
        summary = llm.summarize_text(
            "x" * 50_000,
            model=llm.ModelConfig.TOOL_OUTPUT_SUMMARIZATION_MODEL,
            request_type="tool_output_summarization",
            raise_on_error=True,
        )
        second = llm.summarize_text(
            "y" * 50_000,
            model=llm.ModelConfig.TOOL_OUTPUT_SUMMARIZATION_MODEL,
            request_type="tool_output_summarization",
            raise_on_error=True,
        )

    assert summary == f"SUMMARY by {CHEAP_MODEL}"
    assert second == f"SUMMARY by {CHEAP_MODEL}"
    # Explicit config is tried first every time; the fallback follows.
    assert factory.calls == [_UNSERVABLE, CHEAP_MODEL, _UNSERVABLE, CHEAP_MODEL]
    warnings = [r for r in caplog.records if "has no provider in this deployment" in r.getMessage()]
    assert len(warnings) == 1, "the fallback is reported once per model, not once per output"
    assert _UNSERVABLE in warnings[0].getMessage()
    assert CHEAP_MODEL in warnings[0].getMessage()


def test_usage_row_names_the_model_that_actually_ran(monkeypatch):
    _apply_scenario(monkeypatch)
    llm = _fresh_llm_module()
    monkeypatch.setattr(llm, "get_cached_chat_model", _fake_factory())
    tracked = {}

    def fake_tracked_invoke(summarizer, prompt, **kwargs):
        tracked.update(kwargs)
        return summarizer.invoke(prompt)

    import chat.backend.agent.utils.llm_usage_tracker as tracker

    monkeypatch.setattr(tracker, "tracked_invoke", fake_tracked_invoke)

    out = llm.summarize_text("z" * 1_000, model=_UNSERVABLE, user_id="u-1", session_id="s-1", raise_on_error=True)

    assert out == f"SUMMARY by {CHEAP_MODEL}"
    assert tracked["model_name"] == CHEAP_MODEL
    assert tracked["request_type"] == "tool_output_summarization"


def test_network_and_model_errors_never_trigger_the_fallback(monkeypatch):
    """A 503 / timeout on the configured model propagates (the caller's own truncation
    fallback applies and nothing is cached) — it is not a configuration problem."""
    _apply_scenario(monkeypatch)
    llm = _fresh_llm_module()
    factory = _fake_factory(error=RuntimeError("503 Service Unavailable"))
    monkeypatch.setattr(llm, "get_cached_chat_model", factory)

    with pytest.raises(RuntimeError, match="503"):
        llm.summarize_text("x" * 1_000, model=_UNSERVABLE, raise_on_error=True)
    assert factory.calls == [_UNSERVABLE]


def test_an_unservable_fallback_propagates(monkeypatch):
    """When the auxiliary default cannot be built either, the error reaches the caller
    so ``cap_tool_output`` truncates exactly as before."""
    _apply_scenario(monkeypatch)
    llm = _fresh_llm_module()
    factory = _fake_factory(unavailable=(_UNSERVABLE, CHEAP_MODEL))
    monkeypatch.setattr(llm, "get_cached_chat_model", factory)

    with pytest.raises(ProviderUnavailableError):
        llm.summarize_text("x" * 1_000, model=_UNSERVABLE, raise_on_error=True)
    assert factory.calls == [_UNSERVABLE, CHEAP_MODEL]
    # Default mode (raise_on_error=False) keeps the historical truncation marker.
    assert llm.summarize_text("x" * 5_000, model=_UNSERVABLE).endswith("[truncated due to summarization error]")


def test_a_configured_model_that_works_is_never_replaced(monkeypatch):
    _apply_scenario(monkeypatch)
    llm = _fresh_llm_module()
    factory = _fake_factory(unavailable=())
    monkeypatch.setattr(llm, "get_cached_chat_model", factory)

    out = llm.summarize_text("x" * 1_000, model="openai/gpt-5.6-luna", raise_on_error=True)

    assert out == "SUMMARY by openai/gpt-5.6-luna"
    assert factory.calls == ["openai/gpt-5.6-luna"]


def test_shared_tool_output_summarizer_returns_a_summary_end_to_end(monkeypatch):
    """``summarize_tool_output_once`` (what ``cap_tool_output`` and the context store
    call) returns the fallback's summary instead of raising, so no truncation marker."""
    _apply_scenario(monkeypatch)
    llm = importlib.import_module("chat.backend.agent.llm")
    factory = _fake_factory()
    monkeypatch.setattr(llm, "get_cached_chat_model", factory)
    monkeypatch.setattr(llm, "_unservable_summarizers", set())
    monkeypatch.setattr(llm.ModelConfig, "TOOL_OUTPUT_SUMMARIZATION_MODEL", _UNSERVABLE)
    monkeypatch.setattr(llm.ModelConfig, "AUXILIARY_DEFAULT_MODEL", CHEAP_MODEL)
    tos.clear_summary_cache()
    try:
        out = tos.summarize_tool_output_once("k" * 90_000, tool_name="kubectl", user_id=None)
    finally:
        tos.clear_summary_cache()

    assert out == f"SUMMARY by {CHEAP_MODEL}"
    assert factory.calls == [_UNSERVABLE, CHEAP_MODEL]
