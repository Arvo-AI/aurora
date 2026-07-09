"""
Model catalog: what the user-facing model selector offers.

The selector's list is the **union** of two sources:

1. **Live provider discovery** — for every *configured & available* provider,
   ``get_supported_models()``. This is what makes Aurora's full breadth show up:
   Ollama reports the models you've actually pulled (live ``/api/tags``),
   OpenRouter/OpenAI/Anthropic/Google/Vertex report their mapped models, etc.
   Unavailable providers (missing credentials) contribute nothing, so the list
   reflects what this deployment can actually run.
2. **The curated ``MODEL_CATALOG``** below — display metadata (human name, tier,
   context window, pricing) for the featured/flagship models. This is presentation
   only; it also guarantees the flagships appear even for providers whose
   ``get_supported_models()`` returns nothing (e.g. Bedrock, which serves Claude
   via passthrough and lists no models).

Any model discovered from (1) without a (2) entry still appears — its display
name/provider are derived from the id. And because the backend accepts arbitrary
``provider/model`` ids (passthrough), the selector also lets users enter a custom
id that isn't in either source.

The result can be narrowed per-deployment via the ``ENABLED_MODELS`` env var
(comma-separated model IDs). When unset, the full union is offered — the default
open-source behavior. A deployment that should only expose a subset (e.g. a SaaS
build served exclusively through Bedrock) sets ``ENABLED_MODELS`` to that subset;
no code change required.
"""

import logging
import os
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Display metadata keyed by canonical model ID (matching MODEL_MAPPINGS keys).
# `tier`: free | pro | premium — a rough cost/capability band for the UI.
# `contextLength`: human string shown in the selector.
# `hasReasoning`: whether the model does extended/adaptive thinking.
# `isSlow`: flag heavy-reasoning models so the UI can warn about latency.
MODEL_CATALOG: List[Dict] = [
    {
        "id": "anthropic/claude-opus-4-8",
        "name": "claude-opus-4-8",
        "displayName": "Claude Opus 4.8",
        "provider": "anthropic",
        "tier": "premium",
        "contextLength": "1M",
        "hasReasoning": True,
        "isSlow": True,
        "pricing": "High Cost ($5/$25 per 1M)",
    },
    {
        "id": "anthropic/claude-sonnet-5",
        "name": "claude-sonnet-5",
        "displayName": "Claude Sonnet 5",
        "provider": "anthropic",
        "tier": "pro",
        "contextLength": "1M",
        "hasReasoning": True,
        "pricing": "Medium Cost ($3/$15 per 1M)",
    },
    {
        "id": "anthropic/claude-fable-5",
        "name": "claude-fable-5",
        "displayName": "Claude Fable 5",
        "provider": "anthropic",
        "tier": "premium",
        "contextLength": "1M",
        "hasReasoning": True,
        "isSlow": True,
        "pricing": "Premium Cost ($10/$50 per 1M)",
    },
    {
        "id": "anthropic/claude-opus-4.7",
        "name": "claude-opus-4.7",
        "displayName": "Claude Opus 4.7",
        "provider": "anthropic",
        "tier": "premium",
        "contextLength": "1M",
        "hasReasoning": True,
        "isSlow": True,
        "pricing": "High Cost ($5/$25 per 1M)",
    },
    {
        "id": "anthropic/claude-sonnet-4.6",
        "name": "claude-sonnet-4.6",
        "displayName": "Claude Sonnet 4.6",
        "provider": "anthropic",
        "tier": "pro",
        "contextLength": "1M",
        "hasReasoning": True,
        "pricing": "Medium Cost ($3/$15 per 1M)",
    },
    {
        "id": "anthropic/claude-haiku-4.5",
        "name": "claude-haiku-4.5",
        "displayName": "Claude Haiku 4.5",
        "provider": "anthropic",
        "tier": "free",
        "contextLength": "200K",
        "hasReasoning": True,
        "pricing": "Low Cost ($1/$5 per 1M)",
    },
    {
        "id": "openai/gpt-5.5",
        "name": "gpt-5.5",
        "displayName": "GPT-5.5",
        "provider": "openai",
        "tier": "premium",
        "contextLength": "1M",
        "hasReasoning": True,
        "pricing": "Premium Cost ($5/$30 per 1M)",
    },
    {
        "id": "google/gemini-3.1-pro-preview",
        "name": "gemini-3.1-pro-preview",
        "displayName": "Gemini 3.1 Pro",
        "provider": "google",
        "tier": "pro",
        "contextLength": "1M",
        "hasReasoning": True,
        "pricing": "Medium Cost ($2/$12 per 1M)",
    },
    {
        "id": "google/gemini-3.5-flash",
        "name": "gemini-3.5-flash",
        "displayName": "Gemini 3.5 Flash",
        "provider": "google",
        "tier": "free",
        "contextLength": "1M",
        "hasReasoning": True,
        "pricing": "Low Cost ($0.50/$3 per 1M)",
    },
    {
        "id": "google/gemini-2.5-pro",
        "name": "gemini-2.5-pro",
        "displayName": "Gemini 2.5 Pro",
        "provider": "google",
        "tier": "pro",
        "contextLength": "1M",
        "hasReasoning": True,
        "pricing": "Medium Cost ($1.25/$10 per 1M)",
    },
    {
        "id": "google/gemini-2.5-flash",
        "name": "gemini-2.5-flash",
        "displayName": "Gemini 2.5 Flash",
        "provider": "google",
        "tier": "free",
        "contextLength": "1M",
        "hasReasoning": True,
        "pricing": "Low Cost ($0.30/$2.50 per 1M)",
    },
]


# Fast lookup of curated metadata by model id.
_CATALOG_BY_ID: Dict[str, Dict] = {m["id"]: m for m in MODEL_CATALOG}

# Provider prefix -> display label for providers not covered by a catalog entry.
_PROVIDER_LABELS = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "google": "Google",
    "vertex": "Google",
    "ollama": "Ollama",
    "bedrock": "Bedrock",
    "openrouter": "OpenRouter",
}


def _enabled_model_ids() -> Optional[set]:
    """Parse the ``ENABLED_MODELS`` allowlist.

    Returns a set of allowed model IDs, or ``None`` when the var is unset/empty
    (meaning "no restriction — offer everything"). Whitespace and empty entries
    are ignored so ``ENABLED_MODELS=" a , , b "`` behaves sensibly.
    """
    raw = os.getenv("ENABLED_MODELS", "").strip()
    if not raw:
        return None
    ids = {entry.strip() for entry in raw.split(",") if entry.strip()}
    return ids or None


def _derive_metadata(model_id: str) -> Dict:
    """Build display metadata for a model that isn't in ``MODEL_CATALOG``.

    Live provider discovery surfaces ids we don't have curated entries for
    (every Ollama model you've pulled, the long tail of OpenRouter, etc.). Rather
    than drop them, we derive a reasonable display from the id: the provider
    prefix becomes the group, and the remainder becomes the name.
    """
    if "/" in model_id:
        prefix, name = model_id.split("/", 1)
    else:
        prefix, name = "", model_id
    provider = prefix if prefix in _PROVIDER_LABELS else (prefix or "other")
    # Prettify: "claude-3.5-sonnet" -> "Claude 3.5 Sonnet", "gpt-5.2" -> "Gpt 5.2".
    display = " ".join(part.capitalize() for part in name.replace("_", "-").split("-") if part)
    return {
        "id": model_id,
        "name": name,
        "displayName": display or name,
        "provider": provider,
        "tier": "pro",
        "contextLength": "",
        "hasReasoning": False,
    }


def _canonical_id(model_id: str) -> str:
    """Collapse dash/dot aliases to one key so a model appears once.

    ``MODEL_MAPPINGS`` maps both ``claude-opus-4-8`` and ``claude-opus-4.8`` to the
    same canonical ``openrouter`` name; use that when known, else the id itself.
    """
    try:
        from chat.backend.agent.model_mapper import MODEL_MAPPINGS

        entry = MODEL_MAPPINGS.get(model_id)
        if entry and entry.get("openrouter"):
            return entry["openrouter"]
    except Exception as exc:
        # Non-fatal: fall back to the raw id (it just won't collapse aliases).
        logger.debug("canonicalization skipped for %r: %s", model_id, exc)
    return model_id


# Providers whose models are "featured" (shown in the default view, not just on
# search). These are direct backends where the model list is short and high-intent
# — Ollama surfaces exactly the models you pulled, the direct API providers their
# mapped set. OpenRouter is deliberately excluded: it serves hundreds of models, so
# its long tail is search-only to keep the default view tidy.
_FEATURED_PROVIDERS = {"anthropic", "openai", "google", "vertex", "ollama", "bedrock"}


def _featured_model_ids() -> Optional[set]:
    """Optional ``FEATURED_MODELS`` env override for the default-view set.

    Same shape as ``ENABLED_MODELS``. When set, exactly these ids are featured
    (everything else is search-only), letting ops re-curate the default view per
    deployment without a code change. ``None`` = use the built-in rule.
    """
    raw = os.getenv("FEATURED_MODELS", "").strip()
    if not raw:
        return None
    ids = {entry.strip() for entry in raw.split(",") if entry.strip()}
    return ids or None


def _routable_filter():
    """Return a predicate ``(model_id) -> bool`` for the current provider mode.

    "What you see is what you can use": a model only belongs in the selector if a
    real request for it would find a provider. We ask the *same* router the agent
    uses (``get_provider_for_model`` under the env ``LLM_PROVIDER_MODE``), so the
    list matches actual routing in every mode:

    - ``openrouter`` mode: everything routes through OpenRouter → all pass.
    - ``direct``/``auto``: only ids whose prefix maps to a configured provider.
    - a forced provider (``bedrock``/``vertex``/…): what that provider serves,
      plus the direct-routing fallback for models it can't (e.g. Gemini under
      bedrock → Vertex).

    On any failure resolving the router we fail *open* (allow the model) rather
    than hide everything — a broken filter shouldn't empty the selector.
    """
    try:
        from chat.backend.agent.providers import get_registry

        registry = get_registry()
        mode = os.getenv("LLM_PROVIDER_MODE") or "direct"

        def _routable(model_id: str) -> bool:
            try:
                registry.get_provider_for_model(model_id, mode=mode)
                return True
            except Exception:
                return False

        return _routable
    except Exception as e:
        logger.warning("routability filter unavailable (%s); allowing all models", e)
        return lambda _model_id: True


def _discovered_models() -> List[tuple]:
    """Live ``(model_id, provider_name)`` pairs from every available provider.

    Availability is credential-gated (``provider.is_available()``), so this only
    returns models the deployment can actually run. Ollama reports pulled models
    live; other providers report their mapped set. Failures in one provider don't
    sink the rest. The provider name is retained so the caller can decide which
    models are featured (see ``_FEATURED_PROVIDERS``).
    """
    pairs: List[tuple] = []
    try:
        from chat.backend.agent.providers import get_registry

        registry = get_registry()
        for name, provider in registry.get_available_providers().items():
            try:
                for model_id in provider.get_supported_models() or []:
                    pairs.append((model_id, name))
            except Exception as e:  # a flaky provider must not break the list
                logger.warning("get_supported_models failed for %s: %s", name, e)
    except Exception as e:
        logger.warning("provider discovery unavailable: %s", e)
    return pairs


def get_enabled_models() -> List[Dict]:
    """Return the model list for the selector: curated catalog ∪ live discovery.

    Curated entries come first (newest/flagship, with full metadata), then any
    live-discovered models not already covered (Ollama, OpenRouter long tail, …)
    with derived metadata. The union is de-duplicated by id and, when
    ``ENABLED_MODELS`` is set, filtered to that allowlist.

    Each entry carries ``featured``: True for models shown in the selector's
    default view (curated catalog + direct-provider models like Ollama pulls),
    False for search-only models (the OpenRouter long tail). ``FEATURED_MODELS``
    overrides which ids are featured.
    """
    featured_override = _featured_model_ids()

    def _mark_featured(entry: Dict, is_featured: bool) -> Dict:
        out = dict(entry)
        out["featured"] = (
            entry["id"] in featured_override
            if featured_override is not None
            else is_featured
        )
        return out

    # Only offer models the current LLM_PROVIDER_MODE can actually route — so the
    # selector never shows something that would error on use (e.g. an OpenRouter
    # model while in bedrock mode, or an ollama id while in openrouter mode).
    is_routable = _routable_filter()

    seen = set()
    models: List[Dict] = []

    # 1. Curated catalog first — preserves the flagship ordering and rich metadata.
    #    Always featured (unless a FEATURED_MODELS override says otherwise).
    for entry in MODEL_CATALOG:
        key = _canonical_id(entry["id"])
        if key not in seen and is_routable(entry["id"]):
            seen.add(key)
            models.append(_mark_featured(entry, True))

    # 2. Live discovery — everything this deployment can actually run. De-dupe by
    #    canonical id so dash/dot aliases of an already-listed model don't repeat.
    #    Featured only when the source provider is a direct backend (not OpenRouter),
    #    so Ollama/direct models join the default view but the OpenRouter tail is
    #    search-only.
    for model_id, provider_name in _discovered_models():
        key = _canonical_id(model_id)
        if key not in seen and is_routable(model_id):
            seen.add(key)
            base = _CATALOG_BY_ID.get(model_id) or _derive_metadata(model_id)
            models.append(_mark_featured(base, provider_name in _FEATURED_PROVIDERS))

    allowed = _enabled_model_ids()
    if allowed is None:
        return models

    filtered = [m for m in models if m["id"] in allowed]
    if not filtered:
        # An allowlist that matches nothing is almost certainly a config error
        # (typo in a model ID). Log loudly and fall back to the full list so the
        # selector isn't left empty.
        logger.warning(
            "ENABLED_MODELS=%r matched no available models; serving full list",
            os.getenv("ENABLED_MODELS"),
        )
        return models
    return filtered
