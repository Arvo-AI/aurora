"""Memory selector uses a fast model and a bounded candidate list."""

import asyncio
from datetime import datetime, timezone

from chat.backend.agent.llm import ModelConfig
from services.memory.injector import (
    _MAX_SELECTOR_CANDIDATES,
    _shortlist_entries,
    _select_relevant_memories_async,
)


def _clear_selector_env(monkeypatch):
    monkeypatch.delenv("MEMORY_SELECTOR_MODEL", raising=False)


def test_explicit_selector_model_wins(monkeypatch):
    monkeypatch.setenv("MEMORY_SELECTOR_MODEL", "openai/gpt-4o-mini")
    monkeypatch.setenv("LLM_PROVIDER_MODE", "bedrock")
    assert ModelConfig.memory_selector_model() == "openai/gpt-4o-mini"


def test_bedrock_selector_is_haiku_not_rca(monkeypatch):
    _clear_selector_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER_MODE", "bedrock")
    chosen = ModelConfig.memory_selector_model()
    assert chosen == "anthropic/claude-haiku-5.5"
    assert chosen != "anthropic/claude-opus-5.5"


def test_openrouter_selector_is_flash_lite(monkeypatch):
    _clear_selector_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER_MODE", "openrouter")
    assert ModelConfig.memory_selector_model() == "google/gemini-3.5-flash-lite"


def test_direct_mode_follows_main_provider(monkeypatch):
    _clear_selector_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER_MODE", "direct")
    monkeypatch.setenv("MAIN_MODEL", "google/gemini-3.8-flash")
    assert ModelConfig.memory_selector_model() == "google/gemini-3.5-flash-lite"

    monkeypatch.setenv("MAIN_MODEL", "openai/gpt-5.6-sol")
    assert ModelConfig.memory_selector_model() == "openai/gpt-5.6-luna"

    monkeypatch.setenv("MAIN_MODEL", "anthropic/claude-opus-5.5")
    assert ModelConfig.memory_selector_model() == "anthropic/claude-haiku-5.5"


def test_ollama_selector_stays_on_the_installed_model(monkeypatch):
    _clear_selector_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER_MODE", "ollama")
    monkeypatch.setenv("MAIN_MODEL", "ollama/llama3.1")
    assert ModelConfig.memory_selector_model() == "ollama/llama3.1"


def test_ollama_without_a_local_id_does_not_call_claude(monkeypatch):
    _clear_selector_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER_MODE", "ollama")
    monkeypatch.delenv("MAIN_MODEL", raising=False)
    chosen = ModelConfig.memory_selector_model()
    assert not chosen.startswith("anthropic/")


def test_bedrock_gateway_id_stays_on_bedrock(monkeypatch):
    _clear_selector_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER_MODE", "direct")
    monkeypatch.setenv("MAIN_MODEL", "bedrock/us.anthropic.claude-haiku-5-5")
    assert ModelConfig.memory_selector_model() == "bedrock/us.anthropic.claude-haiku-5-5"


def test_shortlist_keeps_the_overlapping_memory():
    stale = datetime(2020, 1, 1)
    entries = [
        {
            "category": "context",
            "title": f"note-{i}",
            "description": "unrelated office note",
            "updated_at": stale,
        }
        for i in range(120)
    ]
    entries.append(
        {
            "category": "runbook",
            "title": "Redis failover",
            "description": "promote the replica when redis primary is down",
            "updated_at": stale,
        }
    )

    kept = _shortlist_entries("redis primary is down", entries, _MAX_SELECTOR_CANDIDATES)
    titles = [e["title"] for e in kept]
    assert len(kept) == _MAX_SELECTOR_CANDIDATES
    assert titles[0] == "Redis failover"
    assert "note-119" not in titles


def test_shortlist_matches_unspaced_cjk():
    stale = datetime(2020, 1, 1)
    entries = [
        {
            "category": "context",
            "title": f"笔记{i}",
            "description": "无关内容",
            "updated_at": stale,
        }
        for i in range(80)
    ]
    entries.append(
        {
            "category": "runbook",
            "title": "数据库故障处理",
            "description": "主节点宕机时切换副本",
            "updated_at": stale,
        }
    )

    kept = _shortlist_entries("数据库主节点宕机", entries, _MAX_SELECTOR_CANDIDATES)
    assert kept[0]["title"] == "数据库故障处理"


def test_shortlist_with_no_overlap_keeps_the_newest():
    old = datetime(2020, 1, 1)
    new = datetime(2026, 6, 1, tzinfo=timezone.utc)
    entries = [
        {"category": "context", "title": f"note-{i}", "description": "alpha", "updated_at": old}
        for i in range(150)
    ]
    entries.append(
        {"category": "context", "title": "just-added", "description": "beta", "updated_at": new}
    )

    kept = _shortlist_entries("zzzz-no-shared-tokens", entries, _MAX_SELECTOR_CANDIDATES)
    assert kept[0]["title"] == "just-added"
    assert len(kept) == _MAX_SELECTOR_CANDIDATES


def test_selector_prompt_is_capped(monkeypatch):
    monkeypatch.setenv("MEMORY_SELECTOR_MODEL", "anthropic/claude-haiku-4.5")
    stale = datetime(2024, 1, 1)
    entries = [
        {
            "category": "context",
            "title": f"note-{i}",
            "description": "unrelated",
            "updated_at": stale,
        }
        for i in range(100)
    ]
    entries.append(
        {
            "category": "runbook",
            "title": "Redis failover",
            "description": "redis primary",
            "updated_at": stale,
        }
    )
    captured = {}

    class _Response:
        content = '{"selected": [{"category": "runbook", "title": "Redis failover"}]}'

    class _LLM:
        async def ainvoke(self, messages, config=None):
            captured["prompt"] = messages[1].content
            captured["run_name"] = (config or {}).get("run_name")
            return _Response()

    def _fake_create(model, temperature=0.0, streaming=False, max_tokens=256):
        captured["model"] = model
        return _LLM()

    monkeypatch.setattr("services.memory.injector.create_chat_model", _fake_create)
    selected = asyncio.run(
        _select_relevant_memories_async("redis is down", entries)
    )

    assert captured["model"] == "anthropic/claude-haiku-4.5"
    assert captured["run_name"] == "memory_selector"
    assert "Redis failover" in captured["prompt"]
    assert 'note-99' not in captured["prompt"]
    assert captured["prompt"].count("\n- [") <= _MAX_SELECTOR_CANDIDATES
    assert [e["title"] for e in selected] == ["Redis failover"]


def test_openai_selector_asks_for_low_reasoning(monkeypatch):
    monkeypatch.setenv("MEMORY_SELECTOR_MODEL", "openai/gpt-5.6-luna")
    captured = {}

    class _Response:
        content = '{"selected": []}'

    class _LLM:
        async def ainvoke(self, messages, config=None):
            return _Response()

    def _fake_create(model, temperature=0.0, streaming=False, max_tokens=256, **kwargs):
        captured["kwargs"] = kwargs
        return _LLM()

    monkeypatch.setattr("services.memory.injector.create_chat_model", _fake_create)
    asyncio.run(
        _select_relevant_memories_async(
            "hello",
            [{"category": "context", "title": "Note", "description": "x", "updated_at": datetime(2024, 1, 1)}],
        )
    )
    assert captured["kwargs"]["reasoning"] == {"effort": "low"}


def _capture_selector_kwargs(monkeypatch, model: str):
    monkeypatch.setenv("MEMORY_SELECTOR_MODEL", model)
    captured = {}

    class _Response:
        content = '{"selected": []}'

    class _LLM:
        async def ainvoke(self, messages, config=None):
            return _Response()

    def _fake_create(model, temperature=0.0, streaming=False, max_tokens=256, **kwargs):
        captured["kwargs"] = kwargs
        return _LLM()

    monkeypatch.setattr("services.memory.injector.create_chat_model", _fake_create)
    asyncio.run(
        _select_relevant_memories_async(
            "hello",
            [{"category": "context", "title": "Note", "description": "x", "updated_at": datetime(2024, 1, 1)}],
        )
    )
    return captured["kwargs"]


def test_gpt4_pin_does_not_send_reasoning_effort(monkeypatch):
    kwargs = _capture_selector_kwargs(monkeypatch, "openai/gpt-4.1")
    assert "reasoning" not in kwargs


def test_gemini_25_flash_lite_does_not_send_thinking_level(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER_MODE", "google")
    monkeypatch.delenv("GEMINI_DISABLE_THINKING", raising=False)
    kwargs = _capture_selector_kwargs(monkeypatch, "google/gemini-2.5-flash-lite")
    assert "thinking_level" not in kwargs


def test_gemini_35_flash_lite_pins_minimal_thinking(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER_MODE", "google")
    monkeypatch.delenv("GEMINI_DISABLE_THINKING", raising=False)
    kwargs = _capture_selector_kwargs(monkeypatch, "google/gemini-3.5-flash-lite")
    assert kwargs["thinking_level"] == "minimal"
