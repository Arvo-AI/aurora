"""
Extensibility hooks for Aurora server.

Default implementations are no-ops. To provide custom implementations
(e.g., billing enforcement), set AURORA_HOOKS_MODULE to a Python module:

    AURORA_HOOKS_MODULE=utils.hooks_billing

The module must define functions matching the signatures below.
Missing functions fall back to the defaults.
"""

import importlib
import logging
import os
import threading
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_hooks_module = None
_hooks_loaded = False


# --- Default no-op implementations ---

def _default_before_llm_call(org_id: Optional[str], user_id: str) -> Tuple[bool, Optional[str]]:
    """Called before any LLM API call. Return (False, message) to block."""
    return True, None


def _default_after_llm_call(org_id: Optional[str], user_id: str, cost_usd: float) -> None:
    """Called after an LLM call completes with known cost."""
    pass


def _default_on_plan_change(org_id: str, new_plan: str) -> None:
    """Called when an organization's plan changes."""
    pass


# Explicit registry — only these names are valid hook points
_HOOK_REGISTRY = {
    "before_llm_call": _default_before_llm_call,
    "after_llm_call": _default_after_llm_call,
    "on_plan_change": _default_on_plan_change,
}


# --- Hook loader ---

def _load_hooks():
    global _hooks_module, _hooks_loaded
    if _hooks_loaded:
        return
    with _lock:
        if _hooks_loaded:
            return
        _hooks_loaded = True

        module_path = os.environ.get("AURORA_HOOKS_MODULE")
        if not module_path:
            return

        try:
            _hooks_module = importlib.import_module(module_path)
            logger.info("Loaded custom hooks from %s", module_path)
        except Exception as e:
            logger.critical(
                "AURORA_HOOKS_MODULE='%s' failed to import: %s — "
                "billing enforcement may be inactive!", module_path, e
            )


def get_hook(name: str):
    """Get a hook function by name. Returns a safe callable that never raises."""
    _load_hooks()

    if name not in _HOOK_REGISTRY:
        logger.error("Unknown hook '%s' requested", name)
        return _HOOK_REGISTRY.get("before_llm_call", _default_before_llm_call)

    # Try custom module first
    if _hooks_module and hasattr(_hooks_module, name):
        fn = getattr(_hooks_module, name)
    else:
        fn = _HOOK_REGISTRY[name]

    # Wrap in safety net so a broken hook never crashes the caller
    def _safe_hook(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            logger.error("Hook '%s' raised: %s — failing open", name, e)
            return _HOOK_REGISTRY[name](*args, **kwargs)

    return _safe_hook
