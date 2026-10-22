from .context_trim import ContextSafetyMiddleware
from .force_tool import ForceToolChoice as _ForceToolChoice
from .prompt_cache import BedrockPromptCacheMiddleware

# Backward-compatible alias
ContextTrimMiddleware = ContextSafetyMiddleware

__all__ = [
    "BedrockPromptCacheMiddleware",
    "ContextSafetyMiddleware",
    "ContextTrimMiddleware",
    "_ForceToolChoice",
]
