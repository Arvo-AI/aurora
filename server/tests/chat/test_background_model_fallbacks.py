"""Background RCA / enrichment models fall back to MAIN_MODEL when Anthropic is unreachable.

Both selections used to hardcode an Anthropic default with no MAIN_MODEL fallback, so a
self-hosted deployment on another provider (no ANTHROPIC_API_KEY) raised RuntimeError in
create_chat_model as soon as a background RCA or a suggestion enrichment ran.

The fallback is deliberately gated on whether an Anthropic path exists rather than on
MAIN_MODEL's provider: an Anthropic-capable deployment must keep the cheap cost-optimized
default even when MAIN_MODEL is set, since these are the highest-volume background paths.
Bedrock and OpenRouter both serve `anthropic/*` ids, so they count as reachable.

Both module-level values are resolved at import time, so these tests exercise the resolver
functions directly rather than reimporting the modules under a patched environment.
"""
import pytest

from chat.backend.agent.llm import (
    COST_OPTIMIZED_MODEL,
    DEFAULT_MODEL,
    _resolve_rca_model,
    anthropic_default_is_servable,
)
from chat.background.recommender import _resolve_enrichment_model

_NON_ANTHROPIC = "openai/gpt-5"

# Every env var the resolvers read, cleared per-test so the ambient shell/.env can't leak in.
_MODEL_ENV_VARS = (
    "RCA_MODEL",
    "ENRICHMENT_MODEL",
    "MAIN_MODEL",
    "RCA_OPTIMIZE_COSTS",
    "LLM_PROVIDER_MODE",
    "ANTHROPIC_API_KEY",
)


@pytest.fixture(autouse=True)
def _clean_model_env(monkeypatch):
    for var in _MODEL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def anthropic_available(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")


class TestAnthropicDefaultIsServable:
    def test_false_without_any_anthropic_path(self):
        assert anthropic_default_is_servable() is False

    def test_true_with_anthropic_key(self, anthropic_available):
        assert anthropic_default_is_servable() is True

    @pytest.mark.parametrize("mode", ["openrouter", "bedrock"])
    def test_true_for_proxying_provider_modes(self, monkeypatch, mode):
        """OpenRouter proxies every provider; Bedrock's supports_model claims `anthropic/`."""
        monkeypatch.setenv("LLM_PROVIDER_MODE", mode)
        assert anthropic_default_is_servable() is True


class TestResolveRcaModel:
    def test_defaults_to_cost_optimized_model(self, anthropic_available):
        assert _resolve_rca_model() == COST_OPTIMIZED_MODEL

    def test_cost_optimization_disabled_uses_default_model(self, monkeypatch, anthropic_available):
        monkeypatch.setenv("RCA_OPTIMIZE_COSTS", "false")
        assert _resolve_rca_model() == DEFAULT_MODEL

    def test_explicit_rca_model_wins(self, monkeypatch, anthropic_available):
        monkeypatch.setenv("RCA_MODEL", "google/gemini-3.8-flash")
        monkeypatch.setenv("MAIN_MODEL", _NON_ANTHROPIC)
        assert _resolve_rca_model() == "google/gemini-3.8-flash"

    def test_falls_back_to_main_model_without_anthropic(self, monkeypatch):
        """The bug this fixes: no Anthropic key meant a guaranteed RuntimeError."""
        monkeypatch.setenv("MAIN_MODEL", _NON_ANTHROPIC)
        assert _resolve_rca_model() == _NON_ANTHROPIC

    def test_anthropic_key_keeps_cost_optimization(self, monkeypatch, anthropic_available):
        """Guards the cost regression: Anthropic users must not silently lose Haiku."""
        monkeypatch.setenv("MAIN_MODEL", DEFAULT_MODEL)
        assert _resolve_rca_model() == COST_OPTIMIZED_MODEL

    def test_anthropic_key_keeps_cheap_default_even_on_another_provider(
        self, monkeypatch, anthropic_available
    ):
        monkeypatch.setenv("MAIN_MODEL", _NON_ANTHROPIC)
        assert _resolve_rca_model() == COST_OPTIMIZED_MODEL

    @pytest.mark.parametrize("mode", ["openrouter", "bedrock"])
    def test_proxying_modes_keep_cheap_default(self, monkeypatch, mode):
        monkeypatch.setenv("LLM_PROVIDER_MODE", mode)
        monkeypatch.setenv("MAIN_MODEL", _NON_ANTHROPIC)
        assert _resolve_rca_model() == COST_OPTIMIZED_MODEL

    def test_no_anthropic_and_no_main_model_keeps_default(self):
        """Nothing configured at all — behave exactly as before rather than inventing a model."""
        assert _resolve_rca_model() == COST_OPTIMIZED_MODEL


class TestResolveEnrichmentModel:
    def test_defaults_to_cost_optimized_model(self, anthropic_available):
        assert _resolve_enrichment_model() == COST_OPTIMIZED_MODEL

    def test_explicit_enrichment_model_wins(self, monkeypatch, anthropic_available):
        monkeypatch.setenv("ENRICHMENT_MODEL", "google/gemini-3.8-flash")
        monkeypatch.setenv("MAIN_MODEL", _NON_ANTHROPIC)
        assert _resolve_enrichment_model() == "google/gemini-3.8-flash"

    def test_falls_back_to_main_model_without_anthropic(self, monkeypatch):
        monkeypatch.setenv("MAIN_MODEL", _NON_ANTHROPIC)
        assert _resolve_enrichment_model() == _NON_ANTHROPIC

    def test_anthropic_key_keeps_cheap_default(self, monkeypatch, anthropic_available):
        monkeypatch.setenv("MAIN_MODEL", _NON_ANTHROPIC)
        assert _resolve_enrichment_model() == COST_OPTIMIZED_MODEL

    def test_rca_optimize_costs_does_not_affect_enrichment(self, monkeypatch):
        """Enrichment is always light work — RCA_OPTIMIZE_COSTS is not its switch."""
        monkeypatch.setenv("RCA_OPTIMIZE_COSTS", "false")
        monkeypatch.setenv("MAIN_MODEL", _NON_ANTHROPIC)
        assert _resolve_enrichment_model() == _NON_ANTHROPIC
