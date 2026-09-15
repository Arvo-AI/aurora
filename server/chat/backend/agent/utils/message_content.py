"""Flatten LangChain message ``content`` payloads to plain text.

LangChain hands back ``message.content`` as a plain ``str`` for most models, but as
a list of typed blocks for models that emit reasoning alongside their answer:

- Gemini 2.5 / 3.x with ``include_thoughts`` on (google and vertex providers, see
  ``providers.base_provider.apply_gemini_thinking_config``)::

      [{"type": "thinking", "thinking": "..."},
       {"type": "text", "text": "...", "extras": {"signature": "..."}}]

- OpenAI Responses-API reasoning models::

      [{"type": "reasoning", "summary": [{"type": "summary_text", "text": "..."}]},
       {"type": "text", "text": "..."}]

- Anthropic extended thinking: same shape as Gemini (``thinking`` + ``text``).

``str(content)`` on the list form yields a Python repr that no JSON or regex parser
accepts, and ``content.strip()`` raises ``AttributeError``. Both silently broke
Aurora on Gemini deployments (repo descriptions, recommender stages; Aug-Sept 2026).
Every place that reads model output should go through :func:`extract_text_from_content`
instead of re-implementing the block walk.
"""

from typing import Any

_THINKING_BLOCK_TYPES = ("thinking", "reasoning")


def extract_text_from_content(content: Any, include_thinking: bool = False) -> str:
    """Return the text of a message ``content`` payload as one string.

    Args:
        content: ``message.content`` from a LangChain message or stream chunk. A
            ``str``, a list of typed blocks and/or strings, ``None``, or any other
            value.
        include_thinking: When ``True``, ``thinking``/``reasoning`` blocks contribute
            their text (the ``thinking`` field plus any OpenAI ``summary`` entries) in
            stream order. The RCA background chat uses this because the thought stream
            is the investigation progress. When ``False`` (default) they are dropped.

    Returns:
        The concatenated text. ``str`` content is returned untouched and ``None``
        yields ``""``. No whitespace is stripped, so streamed chunks can be joined
        safely; callers that want a trimmed value should ``.strip()`` the result.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)

    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        if block.get("type") in _THINKING_BLOCK_TYPES:
            if not include_thinking:
                continue
            thinking = block.get("thinking")
            if thinking:
                parts.append(str(thinking))
            for item in block.get("summary") or []:
                if isinstance(item, dict):
                    summary_text = item.get("text") or item.get("summary_text")
                    if summary_text:
                        parts.append(str(summary_text))
            continue
        text = block.get("text")
        if text:
            parts.append(str(text))
    return "".join(parts)
