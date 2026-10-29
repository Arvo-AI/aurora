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


def test_thinking_is_shortened_unless_the_client_opts_out():
    assert safety_judge_thinking_overrides("vertex/gemini-3.6-flash")["thinking_level"] == "minimal"


def test_client_can_leave_the_judge_on_the_model_default(monkeypatch):
    monkeypatch.setenv("GUARDRAILS_DISABLE_THINKING", "false")
    assert safety_judge_thinking_overrides("vertex/gemini-3.6-flash") == {}
    assert safety_judge_thinking_overrides("openai/gpt-5") == {}


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
    # Pro rejects 0. 128 is the minimum it accepts.
    assert safety_judge_thinking_overrides("vertex/gemini-2.5-pro")["thinking_budget"] == 128


def test_gemini_38_flash_rejects_minimal():
    assert safety_judge_thinking_overrides("google/gemini-3.8-flash")["thinking_level"] == "low"
    assert safety_judge_thinking_overrides("vertex/gemini-3.6-flash")["thinking_level"] == "minimal"


def test_gpt55_pro_effort_is_medium_not_low():
    assert safety_judge_thinking_overrides("openai/gpt-5.5-pro") == {
        "reasoning": {"effort": "medium"},
    }
    assert safety_judge_thinking_overrides("openai/gpt-5.5-pro-2026-04-23")["reasoning"]["effort"] == "medium"


def test_openrouter_does_not_send_thinking_level(monkeypatch):
    monkeypatch.setenv("GUARDRAILS_DISABLE_THINKING", "true")
    monkeypatch.setenv("LLM_PROVIDER_MODE", "openrouter")
    assert safety_judge_thinking_overrides("google/gemini-3.6-flash") == {}


def test_gpt4_has_no_reasoning_parameter(monkeypatch):
    monkeypatch.setenv("GUARDRAILS_DISABLE_THINKING", "true")
    assert safety_judge_thinking_overrides("openai/gpt-4o-mini") == {}


def test_gpt5_reasoning_drops_to_low():
    assert safety_judge_thinking_overrides("openai/gpt-5.4-mini") == {
        "reasoning": {"effort": "low"},
    }
    assert safety_judge_thinking_overrides("openai/o3")["reasoning"]["effort"] == "low"


def test_haiku_thinking_is_disabled_on_bedrock_and_anthropic():
    assert safety_judge_thinking_overrides(
        "bedrock/anthropic.claude-haiku-4-5-20251001-v1:0"
    ) == {"additional_model_request_fields": {"thinking": {"type": "disabled"}}}
    assert safety_judge_thinking_overrides("anthropic/claude-haiku-4-5") == {
        "thinking": {"type": "disabled"},
    }


def test_haiku_routed_through_bedrock_uses_the_converse_field(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER_MODE", "bedrock")
    assert safety_judge_thinking_overrides("anthropic/claude-haiku-5-5") == {
        "additional_model_request_fields": {"thinking": {"type": "disabled"}},
    }


def test_opus_and_old_haiku_are_not_sent_a_thinking_field():
    # Opus 400s on thinking.type=disabled. Claude 3 Haiku does not think.
    assert safety_judge_thinking_overrides("bedrock/anthropic.claude-opus-4-5") == {}
    assert safety_judge_thinking_overrides("anthropic/claude-3-haiku-20240307") == {}


def test_ollama_thinking_models_are_turned_off_and_plain_llama_is_not():
    assert safety_judge_thinking_overrides("ollama/qwen3:8b") == {"reasoning": False}
    assert safety_judge_thinking_overrides("ollama/deepseek-r1:8b") == {"reasoning": False}
    assert safety_judge_thinking_overrides("ollama/llama3.1:8b") == {}


def test_openrouter_only_disables_weights_that_advertise_thinking(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER_MODE", "openrouter")
    assert safety_judge_thinking_overrides("qwen/qwen3-32b") == {
        "extra_body": {"reasoning": {"enabled": False}},
    }
    assert safety_judge_thinking_overrides("anthropic/claude-haiku-4-5") == {
        "extra_body": {"reasoning": {"enabled": False}},
    }
    assert safety_judge_thinking_overrides("meta-llama/llama-3.1-8b-instruct") == {}
