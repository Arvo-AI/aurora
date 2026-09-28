"""Catalog tables stay consistent with each other and with the frontend picker.

Noah's review on #666 flagged that nothing covered these tables, so a model could be
priced but unselectable (or selectable but unpriced / context-less) without any signal.
Each test below pins one cross-table invariant rather than asserting specific rates, so
the suite doesn't need editing every time a published price changes.
"""
import os
import re

import pytest

from chat.backend.agent.llm import ModelConfig
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
    "openai/gpt-6-astra",
    "openai/gpt-5.6-sol",
    "openai/gpt-5.6-terra",
    "openai/gpt-5.6-luna",
    "google/gemini-3.8-flash",
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


def test_models_that_reject_forcing_have_a_middleware_fallback():
    """Trigger RCA and /action pin tool_choice on their first turn. Opus 5.5 (the
    default) and Fable 5.1 reject that, so ForceToolChoice must carry the auto +
    prompt-directive fallback — see tests/chat/test_force_tool_choice.py. This test
    pins the dependency: if the fallback is ever removed, the default breaks."""
    import inspect

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
    "anthropic/claude-fable-5": (2026, 1, 1),
    "anthropic/claude-opus-4.7": (2026, 1, 1),
    "anthropic/claude-opus-4.6": (2025, 5, 1),
    "anthropic/claude-sonnet-4.6": (2025, 5, 1),
    "anthropic/claude-opus-4-5": (2025, 5, 1),
    "anthropic/claude-haiku-4.5": (2025, 2, 1),
    "anthropic/claude-sonnet-4-5": (2025, 1, 1),
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
