"""Thinking summaries must be safe for the API used by each Gemini family."""
import pytest
from chat.backend.agent.providers.base_provider import apply_gemini_thinking_config


def test_flash_lite_skips_thinking():
    config: dict = {}
    apply_gemini_thinking_config(config, "gemini-3.5-flash-lite")
    assert config == {}


@pytest.mark.parametrize("model", ["gemini-4-argon", "gemini-4-carbon", "gemini-4-carbon-preview"])
@pytest.mark.parametrize("disable", ["", "true"])
def test_gemini_4_suppresses_summaries_without_disabling_reasoning(monkeypatch, model, disable):
    monkeypatch.setenv("GEMINI_DISABLE_THINKING", disable)
    config: dict = {}
    apply_gemini_thinking_config(config, model)
    assert config == {"include_thoughts": False}


def test_flash_gets_thinking_when_enabled(monkeypatch):
    monkeypatch.delenv("GEMINI_DISABLE_THINKING", raising=False)
    config: dict = {}
    apply_gemini_thinking_config(config, "gemini-3.6-flash")
    assert config.get("include_thoughts") is True
