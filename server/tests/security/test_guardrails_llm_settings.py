"""Safety-judge thinking override and call timeout."""
import pytest

from utils.security.command_safety import (
    guardrails_llm_timeout_seconds,
    safety_judge_thinking_overrides,
)


@pytest.fixture(autouse=True)
def _clear_settings(monkeypatch):
    monkeypatch.delenv("GUARDRAILS_LLM_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("GUARDRAILS_DISABLE_THINKING", raising=False)
    monkeypatch.delenv("LLM_PROVIDER_MODE", raising=False)


def test_timeout_defaults_to_10_seconds():
    assert guardrails_llm_timeout_seconds() == 10


def test_timeout_accepts_a_client_value(monkeypatch):
    monkeypatch.setenv("GUARDRAILS_LLM_TIMEOUT_SECONDS", " 20 ")
    assert guardrails_llm_timeout_seconds() == 20


def test_timeout_clamps_and_rejects_garbage(monkeypatch):
    monkeypatch.setenv("GUARDRAILS_LLM_TIMEOUT_SECONDS", "1")
    assert guardrails_llm_timeout_seconds() == 5
    monkeypatch.setenv("GUARDRAILS_LLM_TIMEOUT_SECONDS", "999")
    assert guardrails_llm_timeout_seconds() == 60
    monkeypatch.setenv("GUARDRAILS_LLM_TIMEOUT_SECONDS", "slow")
    assert guardrails_llm_timeout_seconds() == 10


def test_thinking_stays_on_the_model_default_until_the_client_asks():
    assert safety_judge_thinking_overrides("vertex/gemini-3.6-flash") == {}


def test_flash_thinking_drops_to_minimal(monkeypatch):
    monkeypatch.setenv("GUARDRAILS_DISABLE_THINKING", "true")
    assert safety_judge_thinking_overrides("vertex/gemini-3.6-flash") == {
        "thinking_level": "minimal",
        "include_thoughts": False,
    }


def test_pro_and_argon_use_low_because_they_reject_minimal(monkeypatch):
    monkeypatch.setenv("GUARDRAILS_DISABLE_THINKING", "yes")
    assert safety_judge_thinking_overrides("vertex/gemini-3.1-pro-preview")["thinking_level"] == "low"
    assert safety_judge_thinking_overrides("vertex/gemini-4-argon")["thinking_level"] == "low"


def test_gemini_25_uses_a_zero_budget(monkeypatch):
    monkeypatch.setenv("GUARDRAILS_DISABLE_THINKING", "1")
    assert safety_judge_thinking_overrides("vertex/gemini-2.5-flash") == {
        "thinking_budget": 0,
        "include_thoughts": False,
    }


def test_openrouter_does_not_send_thinking_level(monkeypatch):
    monkeypatch.setenv("GUARDRAILS_DISABLE_THINKING", "true")
    monkeypatch.setenv("LLM_PROVIDER_MODE", "openrouter")
    assert safety_judge_thinking_overrides("google/gemini-3.6-flash") == {}


def test_non_gemini_judge_is_unchanged(monkeypatch):
    monkeypatch.setenv("GUARDRAILS_DISABLE_THINKING", "true")
    assert safety_judge_thinking_overrides("openai/gpt-4o-mini") == {}
