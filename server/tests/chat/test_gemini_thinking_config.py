"""Smoke check: flash-lite models must not get thinking config."""
from chat.backend.agent.providers.base_provider import apply_gemini_thinking_config


def test_flash_lite_skips_thinking():
    config: dict = {}
    apply_gemini_thinking_config(config, "gemini-3.5-flash-lite")
    assert config == {}


def test_flash_gets_thinking_when_enabled():
    config: dict = {}
    apply_gemini_thinking_config(config, "gemini-3.6-flash")
    assert config.get("include_thoughts") is True
