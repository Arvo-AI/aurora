"""Catalog tables stay consistent with each other and with the frontend picker.

Review on #666 flagged that nothing covered these tables, so a model could be
priced but unselectable (or selectable but unpriced / context-less) without any signal.
Each test below pins one cross-table invariant rather than asserting specific rates, so
the suite doesn't need editing every time a published price changes.
"""
import os
import re

import pytest

from chat.backend.agent.call_options import CHEAP_MODEL
from chat.backend.agent.llm import ModelConfig, _optimize_costs_enabled
from chat.backend.agent.model_mapper import ModelMapper
from chat.backend.agent.utils.chat_context_manager import ChatContextManager
from chat.backend.agent.utils.llm_usage_tracker import LLMUsageTracker
from chat.backend.agent.utils.model_cutoff_manager import ModelCutoffManager
from chat.backend.agent.utils.openrouter_pricing_service import OpenRouterPricingService

_SELECTOR_PATH = os.path.join(
    os.path.dirname(__file__),
    *([os.pardir] * 3),
    "client", "src", "components", "ModelSelector.tsx",
)

# Providers the picker can surface. `vertex/` rows are synthesized from `google/` ids at
# render time, so they are not expected to carry their own catalog entries everywhere.
_PICKER_PREFIXES = ("openai", "anthropic", "google")


def _selector_source() -> str:
    with open(_SELECTOR_PATH, encoding="utf-8") as fh:
        return fh.read()


def _picker_ids() -> list[str]:
    """Model ids from the ModelSelector `modelCatalog` table.

    Parsed from source rather than duplicated here: a copy in the test would drift and
    silently stop testing the real picker.
    """
    source = _selector_source()
    block = re.search(
        r"const modelCatalog:[^=]*=\s*\[(.*?)\n\];", source, re.DOTALL
    )
    assert block, "could not locate modelCatalog in ModelSelector.tsx"
    ids = re.findall(r"\[\s*'([^']+)'", block.group(1))
    assert ids, "parsed modelCatalog but found no model ids"
    return ids


def _picker_pricing_ids() -> list[str]:
    """Model ids from the ModelSelector `modelPricing` tooltip map."""
    source = _selector_source()
    block = re.search(
        r"const modelPricing: Record<string, string> = \{(.*?)\n\};", source, re.DOTALL
    )
    assert block, "could not locate modelPricing in ModelSelector.tsx"
    return re.findall(r"^\s*'([^']+)':", block.group(1), re.MULTILINE)


@pytest.fixture(scope="module")
def picker_ids() -> list[str]:
    return _picker_ids()


def test_picker_ids_are_unique(picker_ids):
    assert len(picker_ids) == len(set(picker_ids))


def test_every_picker_model_has_static_pricing(picker_ids):
    """A selectable model with no pricing row silently bills at the `default` rate."""
    missing = [
        mid for mid in picker_ids
        if mid.split("/")[0] in _PICKER_PREFIXES
        and mid not in LLMUsageTracker.MODEL_PRICING
    ]
    assert not missing, f"selectable but unpriced: {missing}"


def test_every_picker_model_has_a_tooltip_price(picker_ids):
    """Every row in the dropdown should show a cost hint."""
    tooltip_ids = set(_picker_pricing_ids())
    missing = [mid for mid in picker_ids if mid not in tooltip_ids]
    assert not missing, f"selectable but no pricing tooltip: {missing}"


def test_every_picker_model_has_a_context_limit(picker_ids):
    """Without a limit the context manager falls back to a conservative default and
    summarizes far earlier than the picker's advertised window."""
    limits = ChatContextManager.MODEL_CONTEXT_LIMITS
    missing = [
        mid for mid in picker_ids
        if mid.split("/")[0] in _PICKER_PREFIXES and mid not in limits
    ]
    assert not missing, f"selectable but no context limit: {missing}"


def test_every_picker_model_has_a_known_cutoff(picker_ids):
    """An unknown model falls back to a family-wide cutoff guess, which over- or
    under-triggers automatic web search."""
    known = ModelCutoffManager().models
    missing = [
        mid for mid in picker_ids
        if mid.split("/")[0] in _PICKER_PREFIXES and mid not in known
    ]
    assert not missing, f"selectable but no knowledge cutoff: {missing}"


def test_every_picker_model_resolves_through_the_mapper(picker_ids):
    """Each id must map to a native name for its own provider, or the provider call 404s."""
    for mid in picker_ids:
        prefix = mid.split("/")[0]
        if prefix not in _PICKER_PREFIXES:
            continue
        native = ModelMapper.get_native_name(mid, prefix)
        assert native, f"{mid} has no native mapping for {prefix}"
        assert "/" not in native, f"{mid} resolved to a non-native name: {native}"


def test_no_priced_anthropic_or_openai_model_is_hidden(picker_ids):
    """The inverse of the pricing check: a model we priced but never surfaced is either
    dead config or an oversight in the picker (the mismatch this PR originally fixed)."""
    selectable = set(picker_ids)
    hidden = [
        mid for mid in LLMUsageTracker.MODEL_PRICING
        # Only current-generation ids are expected in the picker; legacy entries are
        # retained purely so historical usage rows still cost out correctly.
        if mid in _CURRENT_GENERATION and mid not in selectable
    ]
    assert not hidden, f"priced and current but not selectable: {hidden}"


# Current-generation ids this PR introduced. Deliberately explicit: MODEL_PRICING also
# holds legacy rows kept for costing historical usage, which must NOT be in the picker.
_CURRENT_GENERATION = {
    "anthropic/claude-fable-5.1",
    "anthropic/claude-fable-5",
    "anthropic/claude-opus-5.5",
    "anthropic/claude-sonnet-5",
    "anthropic/claude-haiku-5.5",
    "openai/gpt-6-astra",
    "openai/gpt-5.6-sol",
    "openai/gpt-5.6-terra",
    "openai/gpt-5.6-luna",
    "google/gemini-3.8-flash",
    "google/gemini-4-argon",
}


def test_openrouter_fallback_pricing_covers_new_models():
    """The OpenRouter fallback table is used when the live API is unreachable."""
    fallback = OpenRouterPricingService().fallback_pricing
    missing = [mid for mid in _CURRENT_GENERATION if mid not in fallback]
    assert not missing, f"no OpenRouter fallback price: {missing}"


def test_default_model_is_fully_registered():
    """The default is the one id every install uses, so it must resolve in every table."""
    default = ModelConfig._DEFAULT_MODEL
    assert default in LLMUsageTracker.MODEL_PRICING
    assert default in ChatContextManager.MODEL_CONTEXT_LIMITS
    assert default in ModelCutoffManager().models
    assert default in _picker_ids(), "default model must be selectable in the picker"


def test_cheap_tier_is_fully_registered():
    """Every background / auxiliary call defaults to the cheap tier, so it must resolve
    in every table and be selectable (users should be able to pick what RCA runs on)."""
    assert ModelConfig.CHEAP_MODEL == "anthropic/claude-haiku-5.5"
    assert ModelConfig.CHEAP_MODEL == CHEAP_MODEL
    assert CHEAP_MODEL in LLMUsageTracker.MODEL_PRICING
    assert CHEAP_MODEL in ChatContextManager.MODEL_CONTEXT_LIMITS
    assert ChatContextManager.get_context_limit(CHEAP_MODEL) == 950000
    assert CHEAP_MODEL in ModelCutoffManager().models
    assert CHEAP_MODEL in _picker_ids(), "cheap tier must be selectable in the picker"
    assert ModelMapper.get_native_name(CHEAP_MODEL, "anthropic") == "claude-haiku-5-5"
    assert ModelMapper.get_native_name("anthropic/claude-haiku-5-5", "openrouter") == CHEAP_MODEL
    # Bedrock EU spelling of the same model shares the window of the clean id.
    assert ChatContextManager.get_context_limit("bedrock/eu.anthropic.claude-haiku-5-5") == 950000


def _execute_fresh(module_name: str):
    """Run a module's source into a throwaway module object under the current env.

    Avoids ``importlib.reload``: reloading would swap the class objects other test
    modules already imported (``monkeypatch.setattr(ModelConfig, ...)`` elsewhere would
    then patch a stale class). The throwaway module never enters ``sys.modules``.
    """
    import importlib
    import importlib.util

    real = importlib.import_module(module_name)
    spec = importlib.util.spec_from_file_location(f"_fresh_{module_name.rsplit('.', 1)[-1]}", real.__file__)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    return fresh


_AUX_MODEL_ENV_VARS = (
    "MAIN_MODEL", "VISION_MODEL", "RCA_MODEL", "RCA_OPTIMIZE_COSTS", "JIRA_RCA_JUDGE_MODEL",
    "SUMMARIZATION_MODEL", "TOOL_OUTPUT_SUMMARIZATION_MODEL", "VISUALIZATION_MODEL",
    "SUGGESTION_MODEL", "EMAIL_REPORT_MODEL", "RECURRENCE_AGENT_MODEL", "MEMORY_AGENT_MODEL",
    "MEMORY_SELECTOR_MODEL", "SEVERITY_MODEL", "GUARDRAILS_LLM_MODEL", "LLM_PROVIDER_MODE",
    "ENRICHMENT_MODEL",
)

# Everything `cheap_tier_routable` reads. Scrubbed in every ModelConfig test below so the
# host's shell (AWS_REGION, a real Anthropic key, ...) cannot decide the outcome.
_ROUTING_ENV_VARS = (
    "LLM_PROVIDER_MODE", "ANTHROPIC_API_KEY", "BEDROCK_BASE_URL", "BEDROCK_REGION",
    "AWS_REGION", "AWS_DEFAULT_REGION",
)

# Every attribute that defaults to ModelConfig.AUXILIARY_DEFAULT_MODEL. RCA_MODEL is in
# the list because RCA_OPTIMIZE_COSTS (default true) also points it there.
_CHEAP_TIER_ATTRS = (
    "RCA_MODEL", "JIRA_RCA_JUDGE_MODEL", "INCIDENT_REPORT_SUMMARIZATION_MODEL",
    "TOOL_OUTPUT_SUMMARIZATION_MODEL", "VISUALIZATION_MODEL", "SUGGESTION_MODEL",
    "ENRICHMENT_MODEL", "EMAIL_REPORT_MODEL", "RECURRENCE_AGENT_MODEL", "MEMORY_AGENT_MODEL",
    "MEMORY_SELECTOR_MODEL", "SEVERITY_MODEL", "GUARDRAILS_MODEL",
)


def _scrub_model_env(monkeypatch):
    for var in _AUX_MODEL_ENV_VARS + _ROUTING_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _fresh_model_config():
    """ModelConfig evaluated under the env the calling test has just set up."""
    return _execute_fresh("chat.backend.agent.llm").ModelConfig


def test_auxiliary_model_defaults_are_the_cheap_tier(monkeypatch):
    """With no overrides every background/classifier variable resolves to CHEAP_MODEL
    and MAIN_MODEL stays on the interactive default (D3 in the implementation plan).
    Evaluated in a scrubbed environment so the host's .env cannot leak in; a dummy
    Anthropic key makes the cheap tier routable under direct routing."""
    _scrub_model_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")

    cfg = _fresh_model_config()
    assert cfg.MAIN_MODEL == cfg._DEFAULT_MODEL == "anthropic/claude-opus-5.5"
    assert cfg.VISION_MODEL == cfg._DEFAULT_MODEL
    assert cfg.AUXILIARY_DEFAULT_MODEL == CHEAP_MODEL
    for name in _CHEAP_TIER_ATTRS:
        assert getattr(cfg, name) == CHEAP_MODEL, name


def test_empty_model_variables_count_as_unset(monkeypatch):
    """Docker Compose forwards ${VAR} as "" when the variable is missing from .env.
    `os.getenv(name, default)` returned that "" verbatim (an empty model id for
    ENRICHMENT_MODEL, cost optimization off for RCA_OPTIMIZE_COSTS); every model
    variable now uses `or` semantics. Routed through Bedrock here so the cheap tier
    is reachable without any API key."""
    _scrub_model_env(monkeypatch)
    for var in _AUX_MODEL_ENV_VARS:
        monkeypatch.setenv(var, "")
    monkeypatch.setenv("LLM_PROVIDER_MODE", "bedrock")
    monkeypatch.setenv("BEDROCK_REGION", "eu-central-1")

    cfg = _fresh_model_config()
    assert cfg.MAIN_MODEL == "anthropic/claude-opus-5.5"
    for name in _CHEAP_TIER_ATTRS:
        assert getattr(cfg, name) == CHEAP_MODEL, name
    assert cfg.RCA_ORCHESTRATOR_MODEL is None
    assert cfg.RCA_SUBAGENT_MODEL is None


def test_model_overrides_and_cost_switch_are_honoured(monkeypatch):
    _scrub_model_env(monkeypatch)
    monkeypatch.setenv("RCA_OPTIMIZE_COSTS", "false")
    monkeypatch.setenv("SUMMARIZATION_MODEL", "google/gemini-2.5-flash")
    monkeypatch.setenv("SEVERITY_MODEL", "openai/gpt-5.6-luna")
    monkeypatch.setenv("LLM_PROVIDER_MODE", "openrouter")

    cfg = _fresh_model_config()
    assert cfg.RCA_MODEL == "anthropic/claude-opus-5.5", "optimize=false -> MAIN's default"
    assert cfg.INCIDENT_REPORT_SUMMARIZATION_MODEL == "google/gemini-2.5-flash"
    assert cfg.TOOL_OUTPUT_SUMMARIZATION_MODEL == "google/gemini-2.5-flash", "follows SUMMARIZATION_MODEL"
    assert cfg.SEVERITY_MODEL == "openai/gpt-5.6-luna"
    assert cfg.GUARDRAILS_MODEL == "google/gemini-2.5-flash-lite", "openrouter keeps its small Gemini judge"
    assert cfg.JIRA_RCA_JUDGE_MODEL == CHEAP_MODEL, "no longer tied to RCA_MODEL"


# --- cheap tier only when the deployment can route it ---------------------------------
#
# The judge and the input rail fail closed and run by default, so handing them an
# `anthropic/` id on an install that cannot reach Anthropic would block every message
# and every command right after an upgrade. Dummy values only.


def test_direct_mode_without_anthropic_key_falls_back_to_main_model(monkeypatch):
    """Ollama-only VM (docs' no-internet tip): LLM_PROVIDER_MODE=direct, no Anthropic key."""
    _scrub_model_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER_MODE", "direct")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
    monkeypatch.setenv("MAIN_MODEL", "ollama/llama3.1")

    cfg = _fresh_model_config()
    assert cfg.MAIN_MODEL == "ollama/llama3.1"
    assert cfg.AUXILIARY_DEFAULT_MODEL == "ollama/llama3.1"
    for name in _CHEAP_TIER_ATTRS:
        assert getattr(cfg, name) == "ollama/llama3.1", name


def test_bedrock_gateway_pinned_ids_fall_back_to_main_model(monkeypatch):
    """Gateway deployment from the docs: direct routing + an explicit bedrock/ id. The
    anthropic/ cheap tier would route to the Anthropic provider, not to the gateway."""
    _scrub_model_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER_MODE", "direct")
    monkeypatch.setenv("BEDROCK_BASE_URL", "https://bedrock-gateway.internal.example.com/v1")
    monkeypatch.setenv("MAIN_MODEL", "bedrock/eu.anthropic.claude-haiku-5-5")

    cfg = _fresh_model_config()
    assert cfg.AUXILIARY_DEFAULT_MODEL == "bedrock/eu.anthropic.claude-haiku-5-5"
    for name in _CHEAP_TIER_ATTRS:
        assert getattr(cfg, name) == "bedrock/eu.anthropic.claude-haiku-5-5", name


def test_forced_bedrock_mode_routes_the_cheap_tier(monkeypatch):
    """LLM_PROVIDER_MODE=bedrock + a region: Bedrock serves anthropic/ ids itself."""
    _scrub_model_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER_MODE", "bedrock")
    monkeypatch.setenv("BEDROCK_REGION", "eu-central-1")
    monkeypatch.setenv("MAIN_MODEL", "anthropic/claude-opus-5.5")

    cfg = _fresh_model_config()
    assert cfg.AUXILIARY_DEFAULT_MODEL == CHEAP_MODEL
    for name in _CHEAP_TIER_ATTRS:
        assert getattr(cfg, name) == CHEAP_MODEL, name


def test_anthropic_key_routes_the_cheap_tier_under_direct_mode(monkeypatch):
    """A non-Claude MAIN_MODEL with an Anthropic key alongside: the cheap tier is
    reachable through prefix routing, so it stays the default."""
    _scrub_model_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER_MODE", "direct")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    monkeypatch.setenv("MAIN_MODEL", "openai/gpt-5.6-sol")

    cfg = _fresh_model_config()
    assert cfg.MAIN_MODEL == "openai/gpt-5.6-sol"
    assert cfg.AUXILIARY_DEFAULT_MODEL == CHEAP_MODEL
    for name in _CHEAP_TIER_ATTRS:
        assert getattr(cfg, name) == CHEAP_MODEL, name


# --- MAIN_MODEL pinned to an explicit bedrock/<id> stays on that Bedrock route ----------
#
# Review finding B1-02: the cheap tier must never move to another provider (residency) or
# to a geo id the pin does not publish (apac. Haiku 5.5 does not exist -> fail-closed
# outage). Dummy values only.


def test_apac_install_pinned_to_jp_profile_keeps_the_cheap_tier_on_bedrock(monkeypatch):
    """LLM_PROVIDER_MODE=bedrock + BEDROCK_REGION=ap-northeast-1 + MAIN=bedrock/jp.…sonnet-4-6:
    every auxiliary variable and GUARDRAILS_MODEL resolve to the jp. Haiku 5.5 profile —
    never to anthropic/claude-haiku-5.5, which the region would derive as apac. (404)."""
    _scrub_model_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER_MODE", "bedrock")
    monkeypatch.setenv("BEDROCK_REGION", "ap-northeast-1")
    monkeypatch.setenv("MAIN_MODEL", "bedrock/jp.anthropic.claude-sonnet-4-6")

    cfg = _fresh_model_config()
    assert cfg.MAIN_MODEL == "bedrock/jp.anthropic.claude-sonnet-4-6"
    assert cfg.AUXILIARY_DEFAULT_MODEL == "bedrock/jp.anthropic.claude-haiku-5-5"
    for name in _CHEAP_TIER_ATTRS:
        value = getattr(cfg, name)
        assert value == "bedrock/jp.anthropic.claude-haiku-5-5", name
        assert not value.startswith("anthropic/"), name


def test_eu_bedrock_pin_with_stray_anthropic_key_never_leaves_bedrock(monkeypatch):
    """LLM_PROVIDER_MODE=direct + ANTHROPIC_API_KEY + BEDROCK_REGION=eu-central-1 +
    MAIN=bedrock/eu.…opus-5-5: prefix routing must not send the judge, input rail,
    summaries, memory, severity, ... to the Anthropic API by default."""
    _scrub_model_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER_MODE", "direct")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    monkeypatch.setenv("BEDROCK_REGION", "eu-central-1")
    monkeypatch.setenv("MAIN_MODEL", "bedrock/eu.anthropic.claude-opus-5-5")

    cfg = _fresh_model_config()
    assert cfg.AUXILIARY_DEFAULT_MODEL == "bedrock/eu.anthropic.claude-haiku-5-5"
    for name in _CHEAP_TIER_ATTRS:
        value = getattr(cfg, name)
        assert value.startswith("bedrock/eu."), name
        assert value in ("bedrock/eu.anthropic.claude-haiku-5-5", cfg.MAIN_MODEL), name


def test_bedrock_gateway_pin_under_forced_bedrock_mode_follows_main_model(monkeypatch):
    """LLM_PROVIDER_MODE=bedrock + BEDROCK_BASE_URL + MAIN=bedrock/<gateway-name>: the
    gateway's names are its own, so the cheap tier is MAIN_MODEL."""
    _scrub_model_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER_MODE", "bedrock")
    monkeypatch.setenv("BEDROCK_BASE_URL", "https://bedrock-gateway.internal.example.com/v1")
    monkeypatch.setenv("BEDROCK_REGION", "eu-central-1")
    monkeypatch.setenv("MAIN_MODEL", "bedrock/claude-sonnet-gateway")

    cfg = _fresh_model_config()
    assert cfg.AUXILIARY_DEFAULT_MODEL == "bedrock/claude-sonnet-gateway"
    for name in _CHEAP_TIER_ATTRS:
        assert getattr(cfg, name) == "bedrock/claude-sonnet-gateway", name


def test_forced_bedrock_in_an_apac_region_with_clean_ids_follows_main_model(monkeypatch):
    """LLM_PROVIDER_MODE=bedrock + BEDROCK_REGION=ap-southeast-1 + clean MAIN_MODEL: the
    region derives apac., which Haiku 5.5 does not publish, so the cheap tier is not
    routable and the fail-closed judge keeps MAIN_MODEL instead of a 404."""
    _scrub_model_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER_MODE", "bedrock")
    monkeypatch.setenv("BEDROCK_REGION", "ap-southeast-1")
    monkeypatch.setenv("MAIN_MODEL", "anthropic/claude-sonnet-4.5")

    cfg = _fresh_model_config()
    assert cfg.AUXILIARY_DEFAULT_MODEL == "anthropic/claude-sonnet-4.5"
    for name in _CHEAP_TIER_ATTRS:
        assert getattr(cfg, name) == "anthropic/claude-sonnet-4.5", name


def test_explicit_values_win_even_when_the_cheap_tier_is_unroutable(monkeypatch):
    _scrub_model_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER_MODE", "direct")
    monkeypatch.setenv("MAIN_MODEL", "ollama/llama3.1")
    monkeypatch.setenv("GUARDRAILS_LLM_MODEL", "ollama/llama3.1:8b")
    monkeypatch.setenv("ENRICHMENT_MODEL", "ollama/qwen2.5:32b")
    monkeypatch.setenv("RCA_OPTIMIZE_COSTS", "false")

    cfg = _fresh_model_config()
    assert cfg.GUARDRAILS_MODEL == "ollama/llama3.1:8b"
    assert cfg.ENRICHMENT_MODEL == "ollama/qwen2.5:32b"
    assert cfg.RCA_MODEL == "anthropic/claude-opus-5.5", "optimize=false keeps the flagship default"
    assert cfg.INCIDENT_REPORT_SUMMARIZATION_MODEL == "ollama/llama3.1", "unset -> MAIN_MODEL here"


def test_rca_optimize_costs_treats_empty_as_true():
    """Compose passes ${RCA_OPTIMIZE_COSTS} through as "" when unset; that is the default."""
    for raw in (None, "", "  ", "true", "TRUE", "1", "yes"):
        assert _optimize_costs_enabled(raw) is True, repr(raw)
    for raw in ("false", "FALSE", "0", "no", "off"):
        assert _optimize_costs_enabled(raw) is False, repr(raw)


def test_haiku_5_5_pricing_has_two_tiers_and_both_spellings_agree():
    """Anthropic bills Haiku 5.5 by prompt length: <=100K at the base rates, >100K at the
    long-context rates for every token of the request (pricing page, read 2026-10-22)."""
    pricing = LLMUsageTracker.MODEL_PRICING
    base = pricing["anthropic/claude-haiku-5.5"]
    assert pricing["anthropic/claude-haiku-5-5"] == base
    assert (base["input"], base["output"], base["cached_input"], base["cache_write"]) == (
        0.0001, 0.0005, 0.00001, 0.000125,
    )
    long_ctx = base["long_context"]
    assert long_ctx["threshold_tokens"] == 100000
    assert (long_ctx["input"], long_ctx["output"], long_ctx["cached_input"], long_ctx["cache_write"]) == (
        0.0005, 0.0025, 0.00005, 0.000625,
    )

    # 10K prompt -> base tier; 200K prompt -> every token at the long-context rate.
    short = LLMUsageTracker.calculate_cost(10_000, 1_000, "anthropic/claude-haiku-5.5",
                                           use_dynamic_pricing=False, provider_mode="direct")
    assert short == pytest.approx(10 * 0.0001 + 1 * 0.0005)
    long = LLMUsageTracker.calculate_cost(200_000, 1_000, "anthropic/claude-haiku-5.5",
                                          use_dynamic_pricing=False, provider_mode="direct")
    assert long == pytest.approx(200 * 0.0005 + 1 * 0.0025)
    # Exactly at the threshold stays on the base tier ("100,000 tokens or fewer").
    edge = LLMUsageTracker.calculate_cost(100_000, 0, "anthropic/claude-haiku-5.5",
                                          use_dynamic_pricing=False, provider_mode="direct")
    assert edge == pytest.approx(100 * 0.0001)


def test_openrouter_pricing_keeps_the_haiku_5_5_long_context_tier(monkeypatch):
    """Review on #740: ``get_model_pricing`` copied only ``cached_input`` from the static
    row, so under LLM_PROVIDER_MODE=openrouter a >100K-token Haiku 5.5 prompt was priced
    at the base rates. The tier must travel with the row (and stay a copy)."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)  # offline: fallback table only
    service = OpenRouterPricingService()
    row = service.get_model_pricing("anthropic/claude-haiku-5.5")
    static = LLMUsageTracker.MODEL_PRICING["anthropic/claude-haiku-5.5"]
    assert row["cached_input"] == static["cached_input"]
    assert row["long_context"] == static["long_context"]
    assert row["long_context"] is not static["long_context"]
    # ...and the tier is what calculate_cost applies on the OpenRouter path.
    monkeypatch.setattr(
        "chat.backend.agent.utils.llm_usage_tracker.get_pricing_service", lambda: service
    )
    long = LLMUsageTracker.calculate_cost(200_000, 1_000, "anthropic/claude-haiku-5.5",
                                          provider_mode="openrouter")
    assert long == pytest.approx(200 * 0.0005 + 1 * 0.0025)
    short = LLMUsageTracker.calculate_cost(10_000, 1_000, "anthropic/claude-haiku-5.5",
                                           provider_mode="openrouter")
    assert short == pytest.approx(10 * 0.0001 + 1 * 0.0005)
    # Rows without a tier are returned as they were.
    assert "long_context" not in service.get_model_pricing("openai/gpt-4.1")


def test_two_tier_pricing_survives_the_bedrock_regional_premium(monkeypatch):
    """The regional premium multiplies every rate; it must skip the nested tier table
    instead of raising (which the outer try/except would turn into a silent $0)."""
    monkeypatch.setenv("BEDROCK_REGION", "eu-central-1")
    monkeypatch.delenv("BEDROCK_REGIONAL_PREMIUM", raising=False)
    cost = LLMUsageTracker.calculate_cost(200_000, 1_000, "anthropic/claude-haiku-5.5",
                                          use_dynamic_pricing=False, provider_mode="bedrock")
    assert cost == pytest.approx((200 * 0.0005 + 1 * 0.0025) * 1.10)
    assert cost > 0


# ---------------------------------------------------------------------------
# Cost observability: Bedrock id normalization, cache-aware cost, configurable premium
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model_id", "expected_key"),
    [
        ("anthropic/claude-opus-5.5", "anthropic/claude-opus-5.5"),
        ("bedrock/eu.anthropic.claude-opus-5-5", "anthropic/claude-opus-5-5"),
        ("bedrock/us.anthropic.claude-haiku-5-5", "anthropic/claude-haiku-5-5"),
        ("eu.anthropic.claude-haiku-5-5", "anthropic/claude-haiku-5-5"),
        ("anthropic.claude-haiku-4-5-20251001-v1:0", "anthropic/claude-haiku-4-5"),
        ("bedrock/eu.anthropic.claude-haiku-4-5-20251001-v1:0", "anthropic/claude-haiku-4-5"),
        ("bedrock/us.anthropic.claude-opus-4-6-v1", "anthropic/claude-opus-4-6"),
        ("bedrock/global.anthropic.claude-fable-5-1", "anthropic/claude-fable-5-1"),
        ("bedrock/eu.anthropic.claude-sonnet-5-5", "anthropic/claude-sonnet-5-5"),
        ("bedrock/eu.anthropic.claude-opus-5", "anthropic/claude-opus-5"),
        ("bedrock/eu.anthropic.claude-opus-4-8", "anthropic/claude-opus-4-8"),
        ("claude-opus-5-5", "anthropic/claude-opus-5-5"),
        ("claude-3-haiku", "anthropic/claude-3-haiku"),
        ("openai/gpt-4.1", "openai/gpt-4.1"),
        ("bedrock/amazon.nova-pro-v1:0", None),
        ("", None),
    ],
)
def test_bedrock_ids_resolve_to_a_priced_key(model_id, expected_key):
    """Every spelling of a Claude id that reaches the tracker (clean, Bedrock geo
    profile with or without the bedrock/ prefix, dated Bedrock id) must price as its
    Anthropic row; previously they fell to the `default` $1/$2 rate."""
    assert LLMUsageTracker._pricing_key(model_id) == expected_key


@pytest.mark.parametrize(
    ("model_id", "expected_limit"),
    [
        ("bedrock/eu.anthropic.claude-haiku-5-5", 950000),
        ("eu.anthropic.claude-opus-5-5", 950000),
        ("bedrock/eu.anthropic.claude-sonnet-5-5", 950000),
        ("bedrock/eu.anthropic.claude-opus-5", 950000),
        ("bedrock/eu.anthropic.claude-opus-4-8", 950000),
        ("bedrock/jp.anthropic.claude-haiku-5-5", 950000),  # geo with no explicit row
        ("anthropic.claude-sonnet-4-5-20250929-v1:0", 950000),  # dated id, dashed version
        ("bedrock/eu.anthropic.claude-haiku-4-5-20251001-v1:0", 180000),  # no row -> bedrock default (200K window)
    ],
)
def test_context_limits_follow_the_same_claude_normalization(model_id, expected_limit):
    """A pinned Bedrock id must get its real window, not the conservative default."""
    assert ChatContextManager.get_context_limit(model_id) == expected_limit


def test_bedrock_geo_id_bills_like_the_clean_id_plus_premium(monkeypatch):
    monkeypatch.setenv("BEDROCK_REGION", "eu-central-1")
    monkeypatch.delenv("BEDROCK_REGIONAL_PREMIUM", raising=False)
    clean = LLMUsageTracker.calculate_cost(10_000, 1_000, "anthropic/claude-opus-5.5",
                                           use_dynamic_pricing=False, provider_mode="bedrock")
    geo = LLMUsageTracker.calculate_cost(10_000, 1_000, "bedrock/eu.anthropic.claude-opus-5-5",
                                         use_dynamic_pricing=False, provider_mode="bedrock")
    assert geo == pytest.approx(clean) == pytest.approx((10 * 0.004 + 1 * 0.020) * 1.10)
    # The default row would have been $0.001/$0.002 per 1K.
    assert geo != pytest.approx(10 * 0.001 + 1 * 0.002)


def test_explicit_bedrock_id_carries_the_premium_under_direct_mode(monkeypatch):
    """LLM_PROVIDER_MODE=direct + a pinned bedrock/<id> is the documented gateway /
    pinning setup; Bedrock still bills it, so the row carries the premium — unless
    the pin is a global. profile."""
    monkeypatch.setenv("BEDROCK_REGION", "eu-central-1")
    monkeypatch.delenv("BEDROCK_REGIONAL_PREMIUM", raising=False)
    pinned = LLMUsageTracker.calculate_cost(10_000, 0, "bedrock/eu.anthropic.claude-haiku-5-5",
                                            use_dynamic_pricing=False, provider_mode="direct")
    assert pinned == pytest.approx(10 * 0.0001 * 1.10)
    global_pin = LLMUsageTracker.calculate_cost(10_000, 0, "bedrock/global.anthropic.claude-haiku-5-5",
                                                use_dynamic_pricing=False, provider_mode="direct")
    assert global_pin == pytest.approx(10 * 0.0001)
    clean_direct = LLMUsageTracker.calculate_cost(10_000, 0, "anthropic/claude-haiku-5.5",
                                                  use_dynamic_pricing=False, provider_mode="anthropic")
    assert clean_direct == pytest.approx(10 * 0.0001)


@pytest.mark.parametrize(
    "model_id",
    ["global.anthropic.claude-haiku-5-5", "bedrock/global.anthropic.claude-haiku-5-5",
     "GLOBAL.anthropic.claude-opus-5-5"],
)
def test_bare_global_profile_ids_never_carry_the_regional_premium(monkeypatch, model_id):
    """Review on #740: a bare ``global.anthropic.…`` id (RCA_ORCHESTRATOR_MODEL style,
    no ``bedrock/`` prefix) is the same global profile and bills at the direct rate,
    whatever region the deployment signs against."""
    monkeypatch.setenv("BEDROCK_REGION", "eu-central-1")
    monkeypatch.delenv("BEDROCK_REGIONAL_PREMIUM", raising=False)
    assert LLMUsageTracker._resolve_regional_premium("bedrock", model_id) == 1.0
    # ...while the regional spelling of the same model still does.
    assert LLMUsageTracker._resolve_regional_premium("bedrock", "eu.anthropic.claude-haiku-5-5") == 1.10


def test_agent_callback_reads_usage_from_generations_then_llm_output():
    """``UsageTrackingCallback`` takes the first generation whose ``usage_metadata``
    carries a non-zero prompt and falls back to OpenAI-style ``llm_output.token_usage``
    only when no generation reports one."""
    from types import SimpleNamespace

    from chat.backend.agent.utils.llm_usage_tracker import UsageTrackingCallback

    def _gen(usage):
        return SimpleNamespace(message=SimpleNamespace(usage_metadata=usage))

    zero = {"input_tokens": 0, "output_tokens": 0}
    real = {"input_tokens": 1_200, "output_tokens": 40, "input_token_details": {"cache_read": 1_000}}
    result = SimpleNamespace(generations=[[_gen(zero)], [_gen(real)]], llm_output=None)
    counts = UsageTrackingCallback._counts_from_llm_result(result)
    assert (counts["input_tokens"], counts["output_tokens"], counts["cached_input_tokens"]) == (1_200, 40, 1_000)

    openai_style = SimpleNamespace(
        generations=[[SimpleNamespace(message=SimpleNamespace(usage_metadata=None))]],
        llm_output={"token_usage": {"prompt_tokens": 300, "completion_tokens": 7}},
    )
    counts = UsageTrackingCallback._counts_from_llm_result(openai_style)
    assert (counts["input_tokens"], counts["output_tokens"]) == (300, 7)

    nothing = SimpleNamespace(generations=[], llm_output={})
    assert UsageTrackingCallback._counts_from_llm_result(nothing)["input_tokens"] == 0


def test_regional_premium_env_override(monkeypatch):
    """BEDROCK_REGIONAL_PREMIUM replaces the hardcoded 1.10; junk, empty and sub-1.0
    values fall back to it, 1.0 disables the premium, global stays premium-free."""
    monkeypatch.setenv("BEDROCK_REGION", "eu-central-1")
    monkeypatch.delenv("BEDROCK_REGIONAL_PREMIUM", raising=False)
    assert LLMUsageTracker._resolve_regional_premium("bedrock", "anthropic/claude-opus-5.5") == 1.10
    monkeypatch.setenv("BEDROCK_REGIONAL_PREMIUM", "1.25")
    assert LLMUsageTracker._resolve_regional_premium("bedrock", "anthropic/claude-opus-5.5") == 1.25
    cost = LLMUsageTracker.calculate_cost(10_000, 0, "anthropic/claude-opus-5.5",
                                          use_dynamic_pricing=False, provider_mode="bedrock")
    assert cost == pytest.approx(10 * 0.004 * 1.25)
    monkeypatch.setenv("BEDROCK_REGIONAL_PREMIUM", "1.0")
    assert LLMUsageTracker._resolve_regional_premium("bedrock", "anthropic/claude-opus-5.5") == 1.0
    for junk in ("", "  ", "ten percent", "0.9", "-1"):
        monkeypatch.setenv("BEDROCK_REGIONAL_PREMIUM", junk)
        assert LLMUsageTracker._resolve_regional_premium("bedrock", "anthropic/claude-opus-5.5") == 1.10, junk
    monkeypatch.setenv("BEDROCK_REGIONAL_PREMIUM", "1.5")
    monkeypatch.setenv("BEDROCK_REGION", "global")
    assert LLMUsageTracker._resolve_regional_premium("bedrock", "anthropic/claude-opus-5.5") == 1.0
    # Vertex keeps the published default; the Bedrock variable does not touch it.
    monkeypatch.setenv("VERTEX_AI_LOCATION", "europe-west1")
    assert LLMUsageTracker._resolve_regional_premium("vertex", "vertex/gemini-2.5-pro") == 1.10
    assert LLMUsageTracker._resolve_regional_premium("anthropic", "anthropic/claude-opus-5.5") == 1.0


def test_cache_tokens_priced_separately():
    """The report's worked example: a 46K-token Opus 5.5 prompt of which 40K is a cache
    read and 3K a cache write. Bedrock's inputTokens would say 3,000; LangChain's
    usage_metadata.input_tokens is the whole prompt (46,000)."""
    cost = LLMUsageTracker.calculate_cost(
        46_000, 0, "anthropic/claude-opus-5.5",
        use_dynamic_pricing=False, provider_mode="anthropic",
        cached_input_tokens=40_000, cache_write_tokens=3_000,
    )
    assert cost == pytest.approx(3 * 0.004 + 40 * 0.0002 + 3 * 0.005)  # 0.035
    # Without the cache breakdown the same prompt would bill 46K at the input rate.
    assert cost < LLMUsageTracker.calculate_cost(
        46_000, 0, "anthropic/claude-opus-5.5", use_dynamic_pricing=False, provider_mode="anthropic",
    )
    # A caller that passes only the non-cached remainder is not billed below the cache tokens.
    remainder_only = LLMUsageTracker.calculate_cost(
        3_000, 0, "anthropic/claude-opus-5.5",
        use_dynamic_pricing=False, provider_mode="anthropic",
        cached_input_tokens=40_000, cache_write_tokens=3_000,
    )
    assert remainder_only == pytest.approx(40 * 0.0002 + 3 * 0.005)


def test_cache_write_falls_back_to_125_percent_of_input():
    """Rows without an explicit write rate (dynamic pricing dicts) use Anthropic's
    5-minute write multiplier instead of the full input rate or $0."""
    cost = LLMUsageTracker.calculate_cost(
        2_000, 0, "openai/gpt-4.1",  # row has no cache_write
        use_dynamic_pricing=False, provider_mode="openai",
        cached_input_tokens=0, cache_write_tokens=1_000,
    )
    assert cost == pytest.approx(1 * 0.002 + 1 * 0.002 * 1.25)


def test_haiku_55_tier_counts_cache_tokens_in_the_prompt_length():
    """Anthropic: 'a request's prompt length counts all of its input tokens, including
    cache reads and cache writes' — a 60K non-cached + 50K cache-read prompt is a
    110K prompt and bills the long-context tier for every token."""
    cost = LLMUsageTracker.calculate_cost(
        110_000, 1_000, "anthropic/claude-haiku-5.5",
        use_dynamic_pricing=False, provider_mode="anthropic",
        cached_input_tokens=50_000,
    )
    assert cost == pytest.approx(60 * 0.0005 + 50 * 0.00005 + 1 * 0.0025)
    short = LLMUsageTracker.calculate_cost(
        90_000, 1_000, "anthropic/claude-haiku-5.5",
        use_dynamic_pricing=False, provider_mode="anthropic",
        cached_input_tokens=50_000,
    )
    assert short == pytest.approx(40 * 0.0001 + 50 * 0.00001 + 1 * 0.0005)


def test_extract_token_counts_reconciles_cache_write_conventions():
    """langchain-aws 1.9 / langchain-anthropic report cache writes either as a total
    (`cache_creation`) or per TTL (`ephemeral_5m/1h_input_tokens`, zeroing the total);
    both must read as the same write count, and a provider that reports both in full
    must not be double counted."""
    extract = LLMUsageTracker.extract_token_counts
    total_only = extract({
        "input_tokens": 46_000, "output_tokens": 500,
        "input_token_details": {"cache_read": 40_000, "cache_creation": 3_000},
    })
    assert total_only == {
        "input_tokens": 46_000, "output_tokens": 500,
        "cached_input_tokens": 40_000, "cache_write_tokens": 3_000,
    }
    per_ttl = extract({
        "input_tokens": 46_000, "output_tokens": 500,
        "input_token_details": {
            "cache_read": 40_000, "cache_creation": 0,
            "ephemeral_5m_input_tokens": 2_000, "ephemeral_1h_input_tokens": 1_000,
        },
    })
    assert per_ttl["cache_write_tokens"] == 3_000
    both_in_full = extract({
        "input_tokens": 46_000, "output_tokens": 500,
        "input_token_details": {
            "cache_read": 40_000, "cache_creation": 3_000,
            "ephemeral_5m_input_tokens": 3_000,
        },
    })
    assert both_in_full["cache_write_tokens"] == 3_000
    openai_style = extract({
        "prompt_tokens": 1_000, "completion_tokens": 20,
        "prompt_tokens_details": {"cached_tokens": 800},
    })
    assert openai_style == {
        "input_tokens": 1_000, "output_tokens": 20,
        "cached_input_tokens": 800, "cache_write_tokens": 0,
    }
    assert extract(None)["input_tokens"] == 0
    assert extract({"input_tokens": "junk", "input_token_details": {"cache_read": None}}) == {
        "input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0, "cache_write_tokens": 0,
    }


def test_track_llm_call_stores_cache_columns(monkeypatch):
    """The cache breakdown reaches the stored row (and the cost) end to end."""
    stored = {}

    def fake_store(usage):
        stored["usage"] = usage
        return True

    monkeypatch.setattr(LLMUsageTracker, "store_usage", classmethod(lambda cls, usage: fake_store(usage)))

    class _Msg:
        content = "ok"
        usage_metadata = {
            "input_tokens": 46_000, "output_tokens": 100,
            "input_token_details": {"cache_read": 40_000, "cache_creation": 3_000},
        }

    assert LLMUsageTracker.track_llm_call(
        user_id="u1", session_id="s1", model_name="anthropic/claude-opus-5.5",
        request_type="unit_test", prompt="hello", response=_Msg(),
        api_provider="anthropic", provider_mode="anthropic",
    )
    usage = stored["usage"]
    assert usage.input_tokens == 46_000
    assert usage.cache_read_input_tokens == 40_000
    assert usage.cache_write_input_tokens == 3_000
    assert usage.estimated_cost == pytest.approx(3 * 0.004 + 40 * 0.0002 + 3 * 0.005 + 0.1 * 0.020)


@pytest.mark.parametrize(
    ("provider_mode", "served_by", "premium"),
    [
        ("bedrock", "bedrock", 1.10),   # Claude under LLM_PROVIDER_MODE=bedrock: Bedrock bills it
        ("direct", "anthropic", 1.0),   # same id under direct routing: the Anthropic API
    ],
)
def test_track_llm_call_without_a_label_uses_the_serving_provider(monkeypatch, provider_mode, served_by, premium):
    """A call tracked with no ``api_provider`` (Jira judge, visualization extractor) is
    labelled with the provider the registry routes the model to — never the old
    ``openrouter`` default nor the raw routing mode — and that label is also the
    pricing mode, so a Bedrock-served row carries the regional premium."""
    import chat.backend.agent.providers as providers_mod

    stored = {}

    def fake_store(usage):
        stored["usage"] = usage
        return True

    monkeypatch.setattr(LLMUsageTracker, "store_usage", classmethod(lambda cls, usage: fake_store(usage)))

    class _Registry:
        def resolve_provider_name(self, model, mode):
            assert mode == provider_mode
            return served_by

    monkeypatch.setattr(providers_mod, "get_registry", lambda: _Registry())
    monkeypatch.setenv("LLM_PROVIDER_MODE", provider_mode)
    monkeypatch.setenv("BEDROCK_REGION", "eu-central-1")
    monkeypatch.delenv("BEDROCK_REGIONAL_PREMIUM", raising=False)

    class _Msg:
        content = "ok"
        usage_metadata = {"input_tokens": 10_000, "output_tokens": 100}

    assert LLMUsageTracker.track_llm_call(
        user_id="u1", session_id=None, model_name="anthropic/claude-haiku-5.5",
        request_type="jira_rca_judge", prompt="ticket", response=_Msg(),
    )
    usage = stored["usage"]
    assert usage.api_provider == served_by
    assert usage.estimated_cost == pytest.approx((10 * 0.0001 + 0.1 * 0.0005) * premium)


@pytest.mark.parametrize(
    ("model_id", "rates"),
    [
        # Anthropic pricing page, read 2026-10-22: input / output / cache read / 5m write per MTok
        ("anthropic/claude-sonnet-5.5", (2.0, 10.0, 0.10, 2.50)),
        ("anthropic/claude-sonnet-5-5", (2.0, 10.0, 0.10, 2.50)),
        ("anthropic/claude-opus-5", (5.0, 25.0, 0.50, 6.25)),
        ("anthropic/claude-opus-4.8", (5.0, 25.0, 0.50, 6.25)),
        ("anthropic/claude-opus-4-8", (5.0, 25.0, 0.50, 6.25)),
        ("anthropic/claude-opus-5.5", (4.0, 20.0, 0.20, 5.0)),
        ("anthropic/claude-haiku-4.5", (1.0, 5.0, 0.10, 1.25)),
    ],
)
def test_new_pricing_rows_match_the_vendor_list(model_id, rates):
    row = LLMUsageTracker.MODEL_PRICING[model_id]
    per_mtok = tuple(row[k] * 1000 for k in ("input", "output", "cached_input", "cache_write"))
    assert per_mtok == pytest.approx(rates)


def test_every_anthropic_row_has_a_cache_write_rate():
    """Cache writes are billed on every Claude model; a row without the rate would
    silently fall back to the 1.25x heuristic."""
    missing = [
        mid for mid, row in LLMUsageTracker.MODEL_PRICING.items()
        if mid.startswith("anthropic/") and "cache_write" not in row
    ]
    assert not missing, missing


def test_models_that_reject_forcing_have_a_middleware_fallback(monkeypatch):
    """Trigger RCA and /action pin tool_choice on their first turn. Opus 5.5 (the
    default) and Fable 5.1 reject that, so ForceToolChoice must carry the auto +
    prompt-directive fallback — see tests/chat/test_force_tool_choice.py. This test
    pins the dependency: if the fallback is ever removed, the default breaks.

    The middleware package imports ``chat.background``, whose ``task.py`` pings Redis
    at import time; stub the client factory (same trick as
    tests/tasks/test_slack_progress.py) so the module loads when this file runs alone.
    """
    import inspect
    from unittest.mock import MagicMock

    from utils.cache import redis_client

    monkeypatch.setattr(redis_client, "get_redis_client", MagicMock)

    from chat.backend.agent.middleware import force_tool

    source = inspect.getsource(force_tool)
    assert "_FALLBACK_DIRECTIVE" in source
    assert "_rejects_forced_tool_choice" in source


def test_gpt56_cutoffs_match_openai_published_date():
    """OpenAI lists Feb 16, 2026 for Sol, Terra and Luna."""
    models = ModelCutoffManager().models
    for mid in ("openai/gpt-5.6-sol", "openai/gpt-5.6-terra", "openai/gpt-5.6-luna"):
        cutoff = models[mid].knowledge_cutoff
        assert (cutoff.year, cutoff.month, cutoff.day) == (2026, 2, 16), mid


# Every date below was read off the vendor's own published page, not inferred:
#   openai/*    -> developers.openai.com/api/docs/models[/<id>] "knowledge cutoff"
#   anthropic/* -> anthropic.com/transparency/model-report + docs models overview,
#                  using the *reliable* knowledge cutoff (not the training cutoff)
#   google/*    -> deepmind.google/models/model-cards/<id> + ai.google.dev model pages
# Anthropic and Google publish two numbers for some models; we pin the one the code
# uses so a future bump has to be deliberate.
_PUBLISHED_CUTOFFS = {
    "openai/gpt-6-astra": (2026, 4, 30),
    "openai/gpt-5.5": (2025, 12, 1),
    "openai/gpt-5.2": (2025, 8, 31),
    "anthropic/claude-fable-5.1": (2026, 6, 1),
    "anthropic/claude-opus-5.5": (2026, 6, 1),
    "anthropic/claude-sonnet-5": (2026, 1, 1),
    # Bedrock model card (model-card-anthropic-claude-haiku-5-5): "Knowledge cutoff: June 2026".
    "anthropic/claude-haiku-5.5": (2026, 6, 1),
    "anthropic/claude-fable-5": (2026, 1, 1),
    "anthropic/claude-opus-4.7": (2026, 1, 1),
    "anthropic/claude-opus-4.6": (2025, 5, 1),
    "anthropic/claude-sonnet-4.6": (2025, 5, 1),
    "anthropic/claude-opus-4-5": (2025, 5, 1),
    "anthropic/claude-haiku-4.5": (2025, 2, 1),
    "anthropic/claude-sonnet-4-5": (2025, 1, 1),
    "google/gemini-4-argon": (2026, 3, 1),
    "google/gemini-3.8-flash": (2026, 3, 1),
    "google/gemini-3.6-flash": (2026, 3, 1),
    "google/gemini-3.5-flash-lite": (2026, 3, 1),
    "google/gemini-3.5-flash": (2025, 1, 1),
    "google/gemini-3.1-pro-preview": (2025, 1, 1),
    "google/gemini-2.5-pro": (2025, 1, 1),
    "google/gemini-2.5-flash": (2025, 1, 1),
}


@pytest.mark.parametrize("model_id,expected", sorted(_PUBLISHED_CUTOFFS.items()))
def test_cutoff_matches_vendor_published_date(model_id, expected):
    """A cutoff that is later than reality makes needs_web_search() skip a search the
    model actually needs, so these must track the vendor pages exactly."""
    cutoff = ModelCutoffManager().models[model_id].knowledge_cutoff
    assert (cutoff.year, cutoff.month, cutoff.day) == expected


def test_vertex_and_google_agree_on_cutoffs():
    """Same weights, two routes — a mismatch means the cutoff depends on which
    provider happened to serve the request."""
    models = ModelCutoffManager().models
    for mid in models:
        if not mid.startswith("vertex/"):
            continue
        twin = mid.replace("vertex/", "google/", 1)
        assert twin in models, f"{mid} has no google/ twin"
        assert (
            models[mid].knowledge_cutoff == models[twin].knowledge_cutoff
        ), f"{mid} and {twin} disagree on knowledge cutoff"


def test_family_fallbacks_never_outrank_a_listed_model():
    """The fallback fires only for unlisted (legacy) ids, so it must be older than the
    oldest id we actually list — otherwise gpt-4o or claude-3 gets credited with
    knowledge it does not have and silently skips web search."""
    manager = ModelCutoffManager()
    for family, fallback in manager.fallback_patterns.items():
        listed = [
            info.knowledge_cutoff
            for mid, info in manager.models.items()
            if family in mid.lower()
        ]
        assert listed, f"no listed models matched family {family}"
        assert fallback <= min(listed), (
            f"{family} fallback {fallback.date()} is newer than the oldest listed "
            f"model ({min(listed).date()})"
        )
