"""Self-check for Anthropic prompt-cache control on the agent system prompt.

Verifies build_cached_system_prompt:
  (a) adds a cache_control ephemeral breakpoint for an Anthropic model + large
      prompt when caching is explicitly enabled,
  (b) no-ops (returns the original str) for a non-Anthropic model even when
      caching is enabled (Google/OpenAI must never get a cache_control block),
  (c) no-ops for a prompt below the minimum cacheable size,
  (d) no-ops when caching is disabled,
  (e) no-ops by default (PROMPT_CACHING_ENABLED unset => off).

Import-light: stubs langchain_core.messages so it runs without the full server.
Runnable directly (python test_cache_control.py) or under pytest.
"""

import contextlib
import importlib.util
import os
import sys
import types


def _load_module():
    """Load cache_control.py with a minimal SystemMessage stub."""
    server_dir = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
    module_path = os.path.join(
        server_dir, "chat", "backend", "agent", "utils", "cache_control.py"
    )

    if "langchain_core.messages" not in sys.modules:
        langchain_core = sys.modules.setdefault(
            "langchain_core", types.ModuleType("langchain_core")
        )
        messages_mod = types.ModuleType("langchain_core.messages")

        class SystemMessage:
            def __init__(self, content):
                self.content = content

        messages_mod.SystemMessage = SystemMessage
        langchain_core.messages = messages_mod
        sys.modules["langchain_core.messages"] = messages_mod

    spec = importlib.util.spec_from_file_location("aurora_cache_control", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def _env(key, value):
    """Set/clear an env var and always restore it (hermetic, no pytest dep)."""
    prev = os.environ.get(key)
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = prev


def _run_checks():
    cc = _load_module()
    big = "x" * (cc._MIN_PREFIX_CHARS + 100)
    small = "tiny system prompt"

    with _env("PROMPT_CACHING_ENABLED", "true"):
        # (a) Anthropic model + large prompt -> SystemMessage with ephemeral breakpoint
        result = cc.build_cached_system_prompt("anthropic/claude-sonnet-4.6", big)
        assert not isinstance(result, str), "expected SystemMessage for Anthropic + large prompt"
        block = result.content[0]
        assert block["cache_control"] == {"type": "ephemeral"}, block
        assert block["text"] == big
        assert "ttl" not in block["cache_control"], "fixed 5m default must omit ttl field"

        # (b) non-Anthropic model -> unchanged string even with caching ON.
        # Google/OpenAI auto-cache server-side and must never receive cache_control.
        assert cc.build_cached_system_prompt("openai/gpt-5", big) == big
        assert cc.build_cached_system_prompt("google/gemini-3.1-pro", big) == big
        assert cc.build_cached_system_prompt("google/gemini-2.5-flash", big) == big

        # (c) tiny prompt -> unchanged string (would waste a cache write)
        assert cc.build_cached_system_prompt("anthropic/claude-sonnet-4.6", small) == small

    # (d) explicitly disabled -> unchanged string
    with _env("PROMPT_CACHING_ENABLED", "false"):
        assert cc.build_cached_system_prompt("anthropic/claude-sonnet-4.6", big) == big

    # (e) default (unset) is OFF -> unchanged string
    with _env("PROMPT_CACHING_ENABLED", None):
        assert cc.build_cached_system_prompt("anthropic/claude-sonnet-4.6", big) == big


def test_cache_control_behavior():
    _run_checks()


if __name__ == "__main__":
    _run_checks()
    print("cache_control self-check passed")
