"""
Model catalog: display metadata for user-facing model selection.

`MODEL_MAPPINGS` (in model_mapper.py) is the plumbing — it maps model IDs to
native provider formats. This module is the *presentation* layer: the human-
readable name, provider label, context window, and pricing that the model
selector renders. Keeping it separate means the selector never has to hardcode
its own list.

The catalog can be narrowed per-deployment via the ``ENABLED_MODELS`` env var
(comma-separated model IDs). When unset, every catalogued model is offered —
this is the default open-source behavior. A deployment that should only expose
a subset (e.g. a SaaS build served exclusively through Bedrock) sets
``ENABLED_MODELS`` to that subset; no code change required.
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


def get_enabled_models() -> List[Dict]:
    """Return the catalog filtered by the ``ENABLED_MODELS`` allowlist.

    Order is preserved from ``MODEL_CATALOG`` so the selector shows the newest
    and most capable models first. When the allowlist is unset, the full catalog
    is returned unchanged.
    """
    allowed = _enabled_model_ids()
    if allowed is None:
        return list(MODEL_CATALOG)

    filtered = [model for model in MODEL_CATALOG if model["id"] in allowed]
    if not filtered:
        # An allowlist that matches nothing is almost certainly a config error
        # (typo in a model ID). Log loudly and fall back to the full catalog so
        # the selector isn't left empty.
        logger.warning(
            "ENABLED_MODELS=%r matched no catalogued models; serving full catalog",
            os.getenv("ENABLED_MODELS"),
        )
        return list(MODEL_CATALOG)
    return filtered
