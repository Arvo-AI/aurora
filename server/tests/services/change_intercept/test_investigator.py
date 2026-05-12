"""Unit tests for the standalone investigator's helpers.

End-to-end LLM behaviour is exercised by the calibration phase, not
unit tests — we can't mock the LLM cheaply enough to make a single-shot
test meaningful. Here we pin the two pieces that have actual logic:

  - ``_parse_json``: tolerates markdown fences + leading/trailing prose,
    safely returns ``{}`` on garbage.
  - ``_extract_text``: handles every LangChain response shape we see in
    practice (string, AIMessage.content as str, AIMessage.content as
    list-of-blocks).

The full ``invoke`` is integration-tested via mocking ``create_chat_model``
in a couple of representative scenarios.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from services.change_intercept import investigator
from services.change_intercept.investigator import (
    InvestigatorResult,
    _extract_text,
    _parse_json,
    invoke,
)


# ─── _parse_json ────────────────────────────────────────────────────


def test_parse_json_handles_bare_object() -> None:
    assert _parse_json('{"verdict": "approve"}')["verdict"] == "approve"


def test_parse_json_strips_json_fence() -> None:
    text = '```json\n{"verdict": "approve"}\n```'
    assert _parse_json(text)["verdict"] == "approve"


def test_parse_json_strips_unlabelled_fence() -> None:
    text = '```\n{"verdict": "approve"}\n```'
    assert _parse_json(text)["verdict"] == "approve"


def test_parse_json_tolerates_leading_prose() -> None:
    text = 'Sure thing! Here you go:\n```json\n{"verdict": "approve"}\n```'
    assert _parse_json(text)["verdict"] == "approve"


def test_parse_json_recovers_object_from_naked_prose_wrapping() -> None:
    text = 'OK: {"verdict": "approve", "findings": []} done'
    assert _parse_json(text)["verdict"] == "approve"


def test_parse_json_returns_empty_for_unparseable_input() -> None:
    assert _parse_json("not json") == {}
    assert _parse_json("") == {}


def test_parse_json_returns_empty_for_non_object_jsons() -> None:
    # Lists / strings / numbers must not be returned as dicts.
    assert _parse_json('["a","b"]') == {}
    assert _parse_json('"just a string"') == {}
    assert _parse_json('42') == {}


# ─── _extract_text ──────────────────────────────────────────────────


def test_extract_text_passes_string_through() -> None:
    assert _extract_text("hello") == "hello"


def test_extract_text_reads_string_content_attribute() -> None:
    msg = SimpleNamespace(content="hello")
    assert _extract_text(msg) == "hello"


def test_extract_text_concatenates_list_content_blocks() -> None:
    msg = SimpleNamespace(content=[{"text": "a"}, {"text": "b"}])
    assert _extract_text(msg) == "ab"


def test_extract_text_skips_non_text_blocks() -> None:
    msg = SimpleNamespace(content=[{"text": "a"}, {"image": "ignore"}, "raw"])
    assert _extract_text(msg) == "araw"


def test_extract_text_returns_empty_for_unknown_shape() -> None:
    assert _extract_text(None) == ""
    assert _extract_text(42) == ""


# ─── invoke() — mocked LLM ─────────────────────────────────────────


def test_invoke_returns_parsed_json_when_llm_returns_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: LLM returns clean JSON, we parse it and pack a result."""
    fake_response = SimpleNamespace(content='{"verdict":"approve","findings":[]}')
    fake_llm = MagicMock(invoke=lambda _: fake_response)

    def fake_create_chat_model(model: str, **_kw: Any) -> Any:
        return fake_llm

    # Patch the lazy-imported function — the real provider stack is
    # absent in the test container.
    import chat.backend.agent.providers as providers_mod

    monkeypatch.setattr(providers_mod, "create_chat_model", fake_create_chat_model)

    result = invoke("test prompt", model="anthropic/claude-haiku-4.5")
    assert isinstance(result, InvestigatorResult)
    assert result.ok is True
    assert result.parsed["verdict"] == "approve"
    assert result.llm_model == "anthropic/claude-haiku-4.5"
    assert result.duration_ms >= 0


def test_invoke_returns_not_ok_on_provider_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("provider down")

    import chat.backend.agent.providers as providers_mod

    monkeypatch.setattr(providers_mod, "create_chat_model", boom)

    result = invoke("test prompt")
    assert result.ok is False
    assert "create_model_failed" in (result.error or "")


def test_invoke_returns_not_ok_on_invoke_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_llm = MagicMock()
    fake_llm.invoke.side_effect = TimeoutError("upstream timeout")
    monkeypatch.setattr(
        "chat.backend.agent.providers.create_chat_model",
        lambda *_a, **_kw: fake_llm,
    )
    result = invoke("test prompt")
    assert result.ok is False
    assert "invoke_failed" in (result.error or "")


def test_invoke_returns_empty_parsed_for_non_json_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_response = SimpleNamespace(content="not json at all")
    fake_llm = MagicMock(invoke=lambda _: fake_response)
    monkeypatch.setattr(
        "chat.backend.agent.providers.create_chat_model",
        lambda *_a, **_kw: fake_llm,
    )
    result = invoke("test prompt")
    assert result.ok is False
    assert result.parsed == {}
    assert result.raw_text == "not json at all"
