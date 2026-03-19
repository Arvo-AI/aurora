from .context_trim import ContextSafetyMiddleware

# Backward-compatible alias
ContextTrimMiddleware = ContextSafetyMiddleware

__all__ = ["ContextSafetyMiddleware", "ContextTrimMiddleware"]
