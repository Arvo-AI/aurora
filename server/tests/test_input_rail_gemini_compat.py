from __future__ import annotations

import asyncio

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from guardrails.input_rail import _GeminiMaxTokensCompat


class _RecordingModel(BaseChatModel):
    """Minimal BaseChatModel that records kwargs ``_generate``/``_agenerate`` saw."""

    last_generate_kwargs: dict | None = None
    _next_content: list | str = "No"

    @property
    def _llm_type(self) -> str:
        return "recording"

    def _generate(self, messages, stop=None, **kwargs):
        self.last_generate_kwargs = dict(kwargs)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self._next_content))])

    async def _agenerate(self, messages, stop=None, **kwargs):
        self.last_generate_kwargs = dict(kwargs)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self._next_content))])


def test_rename_translates_max_tokens():
    inner = _RecordingModel()
    wrapped = _GeminiMaxTokensCompat(inner=inner)

    asyncio.run(wrapped._agenerate([HumanMessage(content="hi")], max_tokens=3, temperature=0.0))

    assert inner.last_generate_kwargs == {"max_output_tokens": 3, "temperature": 0.0}


def test_rename_leaves_other_kwargs_alone():
    inner = _RecordingModel()
    wrapped = _GeminiMaxTokensCompat(inner=inner)

    asyncio.run(wrapped._agenerate([HumanMessage(content="hi")], temperature=0.5, top_p=0.9))

    assert inner.last_generate_kwargs == {"temperature": 0.5, "top_p": 0.9}


def test_rename_does_not_overwrite_explicit_max_output_tokens():
    inner = _RecordingModel()
    wrapped = _GeminiMaxTokensCompat(inner=inner)

    asyncio.run(
        wrapped._agenerate([HumanMessage(content="hi")], max_tokens=3, max_output_tokens=64)
    )

    assert inner.last_generate_kwargs == {"max_tokens": 3, "max_output_tokens": 64}


def test_flatten_collapses_gemini_thinking_blocks():
    inner = _RecordingModel()
    inner._next_content = [
        {"type": "thinking", "thinking": "reasoning..."},
        {"type": "text", "text": "Yes"},
    ]
    wrapped = _GeminiMaxTokensCompat(inner=inner)

    result = asyncio.run(wrapped._agenerate([HumanMessage(content="hi")]))

    assert result.generations[0].message.content == "Yes"


def test_bind_runs_through_wrapper_so_flatten_still_applies():
    inner = _RecordingModel()
    inner._next_content = [{"type": "text", "text": "No"}]
    wrapped = _GeminiMaxTokensCompat(inner=inner)

    bound = wrapped.bind(max_tokens=3)
    result = asyncio.run(bound.ainvoke([HumanMessage(content="hi")]))

    assert result.content == "No"
    assert inner.last_generate_kwargs == {"max_output_tokens": 3}
