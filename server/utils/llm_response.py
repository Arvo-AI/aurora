"""Helpers for reading text out of LLM response content.

Gemini 2.5/3.x thinking models (google and vertex providers) return message
``content`` as a *list* of blocks (``{"type": "thinking", ...}`` +
``{"type": "text", ...}``) rather than a plain string. Calling ``.strip()`` on
that list crashes, and ``str(list)`` silently stringifies the thinking blocks
into garbage that then fails downstream JSON/regex parsing. Route every LLM
reply through :func:`extract_text_from_response` before parsing it.
"""

from typing import Any, List, Union


def _extract_text_part(part: Any) -> str:
    """Extract text from a single content block, dropping thinking/reasoning blocks."""
    # Plain string block — already text.
    if isinstance(part, str):
        return part

    # Structured block — keep everything except thinking/reasoning blocks.
    if isinstance(part, dict) and part.get("type") not in ("thinking", "reasoning"):
        text = part.get("text", "")
        return str(text) if text else ""

    # Thinking/reasoning block (or unknown non-text type) — drop it.
    return ""


def extract_text_from_response(content: Union[str, List[Any], Any]) -> str:
    """Extract the human-readable text from an LLM response's ``content``.

    Handles the three shapes ``content`` can take:
    - ``str``: returned stripped (the common non-thinking case).
    - ``list``: thinking/reasoning blocks are dropped and the remaining text
      blocks are concatenated (Gemini/Anthropic thinking models).
    - anything else: coerced with ``str()`` as a last resort.
    """
    # Plain string — the common non-thinking case.
    if isinstance(content, str):
        return content.strip()

    # List of blocks — thinking model output; keep only the text blocks.
    if isinstance(content, list):
        return "".join(_extract_text_part(part) for part in content).strip()

    # Unexpected shape — coerce rather than crash.
    return str(content).strip()
