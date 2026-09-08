"""Tests for the shared LLM response text extractor.

Gemini 2.5/3.x thinking models (google/vertex) return ``response.content`` as a
list of ``thinking`` + ``text`` blocks. ``str(content)`` stringifies the whole
list (thinking blocks and all), which then breaks downstream JSON/regex parsing.
:func:`extract_text_from_response` must drop the thinking blocks and return only
the real text so callers can parse it.
"""

from utils.llm_response import extract_text_from_response


def test_plain_string_is_stripped():
    assert extract_text_from_response("  hello world  ") == "hello world"


def test_thinking_list_yields_text_only():
    content = [
        {"type": "thinking", "thinking": "Let me reason about this..."},
        {"type": "text", "text": '  {"hypotheses": []}  '},
    ]
    assert extract_text_from_response(content) == '{"hypotheses": []}'


def test_multiple_text_blocks_are_concatenated():
    content = [
        {"type": "text", "text": "foo"},
        {"type": "thinking", "thinking": "ignore me"},
        {"type": "text", "text": "bar"},
    ]
    assert extract_text_from_response(content) == "foobar"


def test_bare_string_parts_are_kept():
    assert extract_text_from_response(["a", "b"]) == "ab"


def test_thinking_only_content_is_empty():
    assert extract_text_from_response([{"type": "thinking", "thinking": "only thoughts"}]) == ""


def test_empty_content_is_empty():
    assert extract_text_from_response("") == ""
    assert extract_text_from_response([]) == ""


def test_non_str_non_list_is_coerced():
    assert extract_text_from_response(None) == "None"
