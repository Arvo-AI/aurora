"""Smoke check: flash-lite models must not get thinking config."""
from chat.backend.agent.providers.base_provider import apply_gemini_thinking_config


def test_flash_lite_skips_thinking():
    config: dict = {}
    apply_gemini_thinking_config(config, "gemini-3.5-flash-lite")
    assert config == {}


def test_argon_gets_thoughts_without_forcing_a_level(monkeypatch):
    # Argon's API default is medium; only preview ids get an explicit level.
    monkeypatch.delenv("GEMINI_DISABLE_THINKING", raising=False)
    config: dict = {}
    apply_gemini_thinking_config(config, "gemini-4-argon")
    assert config.get("include_thoughts") is True
    assert "thinking_level" not in config


def test_flash_gets_thinking_when_enabled(monkeypatch):
    monkeypatch.delenv("GEMINI_DISABLE_THINKING", raising=False)
    config: dict = {}
    apply_gemini_thinking_config(config, "gemini-3.6-flash")
    assert config.get("include_thoughts") is True
