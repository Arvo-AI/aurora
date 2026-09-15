"""The shared text extractor must flatten every content shape LangChain hands back.

``tests/fixtures/gemini_thinking_responses.json`` holds four real gemini-3.6-flash
responses captured through Aurora's google provider with thinking on: the exact
payloads the recommender stages receive on a Gemini deployment (Bombora).
"""
import json
from pathlib import Path

import pytest

from chat.backend.agent.utils.message_content import extract_text_from_content

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "gemini_thinking_responses.json"


def _gemini_responses() -> list[dict]:
    return json.loads(_FIXTURE.read_text())["responses"]


@pytest.mark.parametrize("response", _gemini_responses(), ids=lambda r: r["stage"])
def test_real_gemini_thinking_responses_yield_the_text_block_only(response):
    content = response["content"]
    thinking_block, text_block = content
    assert thinking_block["type"] == "thinking" and text_block["type"] == "text"

    result = extract_text_from_content(content)

    assert result == text_block["text"]
    assert thinking_block["thinking"][:40] not in result


def test_include_thinking_keeps_thought_text_in_stream_order():
    thinking_block, text_block = _gemini_responses()[0]["content"]
    result = extract_text_from_content([thinking_block, text_block], include_thinking=True)
    assert result == thinking_block["thinking"] + text_block["text"]


def test_openai_reasoning_summary_only_when_requested():
    content = [
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "Plan: "}, {"summary_text": "check pods. "}]},
        {"type": "text", "text": "Restart the pod."},
    ]
    assert extract_text_from_content(content) == "Restart the pod."
    assert extract_text_from_content(content, include_thinking=True) == "Plan: check pods. Restart the pod."


def test_string_content_is_returned_untouched():
    assert extract_text_from_content("  keep my spaces  ") == "  keep my spaces  "


def test_none_and_empty_list_yield_empty_string():
    assert extract_text_from_content(None) == ""
    assert extract_text_from_content([]) == ""


def test_thinking_only_content_yields_empty_string():
    assert extract_text_from_content([{"type": "thinking", "thinking": "only thoughts"}]) == ""


def test_string_parts_and_untyped_text_blocks_are_kept():
    content = ["a", {"text": "b"}, {"type": "text", "text": "c"}]
    assert extract_text_from_content(content) == "abc"


def test_blocks_without_text_are_ignored():
    content = [{"type": "image_url", "image_url": {"url": "data:..."}}, 42, None, {"type": "text", "text": "x"}]
    assert extract_text_from_content(content) == "x"


def test_non_string_non_list_content_is_stringified():
    assert extract_text_from_content(123) == "123"
