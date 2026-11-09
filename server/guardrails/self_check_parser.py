"""Verdict parser for the input rail's yes/no self-check.

Why this exists
---------------
NeMo parses self-check answers with ``is_content_safe``: it replaces every run
of non-word characters with a space, splits on spaces and looks for
``yes``/``no`` in the first two tokens only, returning "unsafe" when it finds
neither. A chat model asked to end with ``Answer:`` often replies in Markdown:

    **Answer: No**

    This is an ordinary pull request description...

``**`` becomes a leading space, so the tokens are ``["", "answer", "no", ...]``,
the window is ``["", "answer"]`` and a correct "No, allow it" is read as a
block. On Aurora's own merged PRs that turned 12 of 20 harmless descriptions
into guardrail blocks. Their PR reviews were silently dropped.

Contract
--------
Same return shape as NeMo's parsers: ``[True]`` = allow, ``[False]`` = block.
The verdict is the first word after Markdown and an optional label
(``Answer:``, ``Final answer -``, ``Verdict:``...). ``no``/``safe`` allows,
``yes``/``unsafe`` blocks, and anything else (empty, a refusal, rambling,
truncated thinking) still blocks. This module only reads the verdict more
carefully. It does not make the rail more permissive.
"""

from __future__ import annotations

import re
from typing import List

PARSER_NAME = "aurora_self_check_verdict"

# Words a model puts before its verdict. Skipped, so "Answer: No" reads as
# "No". Deliberately excludes "no", "yes", "safe", "unsafe" and any negation.
_LABEL_WORDS = frozenset({
    "answer", "final", "verdict", "decision", "result", "response", "output",
    "classification", "label", "my", "the", "is",
})
_ALLOW = frozenset({"no", "safe"})
_BLOCK = frozenset({"yes", "unsafe"})

# A label can take up a few words ("My final answer is: No"). Past this many
# words with no verdict, the answer has wandered into prose, so block.
_MAX_LEADING_WORDS = 5

_WORD_RE = re.compile(r"[a-z]+")


def parse_self_check_verdict(response: object) -> List[bool]:
    """Return ``[True]`` to allow the input, ``[False]`` to block it."""
    if not isinstance(response, str):
        return [False]
    for position, word in enumerate(_WORD_RE.findall(response.lower())):
        if position >= _MAX_LEADING_WORDS:
            break
        if word in _ALLOW:
            return [True]
        if word in _BLOCK:
            return [False]
        if word not in _LABEL_WORDS:
            # The answer opens with something that is not a verdict and not a
            # label ("I can't...", "Let me think"). No verdict, so block.
            break
    return [False]
