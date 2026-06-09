"""
Extensibility hooks for Aurora server.

Default implementations are no-ops. To provide custom implementations
(e.g., custom enforcement), set AURORA_HOOKS_MODULE to a Python module:

    AURORA_HOOKS_MODULE=utils.hooks_custom

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
_hooks_module: object = None
_hooks_loaded: bool = False
_hook_cache: dict = {}


# --- Default no-op implementations ---

def _default_before_llm_call(org_id: Optional[str], user_id: str) -> Tuple[bool, Optional[str]]:
    """Called before any LLM API call. Return (False, message) to block."""
    return True, None


def _default_after_llm_call(org_id: Optional[str], user_id: str, metadata: dict) -> None:
    """Called after an LLM call completes with metadata about the call."""
    pass


def _default_before_add_member(org_id: str, current_member_count: int) -> Tuple[bool, Optional[str]]:
    """Called before adding a member to an org. Return (False, message) to block."""
    return True, None


# Explicit registry — only these names are valid hook points
_HOOK_REGISTRY = {
    "before_llm_call": _default_before_llm_call,
    "after_llm_call": _default_after_llm_call,
    "before_add_member": _default_before_add_member,
}


# --- Hook loader ---

def _load_hooks():
    global _hooks_module, _hooks_loaded
    if _hooks_loaded:
        return
    with _lock:
        if _hooks_loaded:
            return

        module_path = os.environ.get("AURORA_HOOKS_MODULE")
        if not module_path:
            _hooks_loaded = True
            return

        try:
            _hooks_module = importlib.import_module(module_path)
            logger.info("Loaded custom hooks from %s", module_path)
        except Exception as e:
            logger.critical(
                "AURORA_HOOKS_MODULE='%s' failed to import: %s — "
                "custom hooks may be inactive!", module_path, e
            )
        _hooks_loaded = True


def get_hook(name: str):
    """Get a hook function by name. Returns a safe callable that never raises."""
    _load_hooks()

    if name not in _HOOK_REGISTRY:
        raise ValueError(f"Unknown hook '{name}' — valid hooks: {list(_HOOK_REGISTRY.keys())}")

    # Return cached closure if available
    if name in _hook_cache:
        return _hook_cache[name]

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

    _hook_cache[name] = _safe_hook
    return _safe_hook
