"""Parse action items from a postmortem markdown document.

Looks for an ``Action Items`` section (case-insensitive, any heading level)
and extracts ``- [ ]`` / ``- [x]`` checklist lines, with heuristics for
assignee and due-date hints.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional, TypedDict

logger = logging.getLogger(__name__)


class ActionItem(TypedDict, total=False):
    text: str
    assignee_hint: Optional[str]
    due_hint: Optional[str]
    checked: bool


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_CHECKBOX_RE = re.compile(r"^\s*[-*]\s+\[( |x|X)\]\s+(.*)$")
_ACTION_HEADING_RE = re.compile(r"^\s*action\s+items\s*$", re.IGNORECASE)

_DUE_PATTERNS = [
    re.compile(r"\(\s*due[:\s]+(\d{4}-\d{2}-\d{2})\s*\)", re.IGNORECASE),
    re.compile(r"\bdue[:\s]+(\d{4}-\d{2}-\d{2})\b", re.IGNORECASE),
    re.compile(r"\(\s*due\s+(\d{4}-\d{2}-\d{2})\s*\)", re.IGNORECASE),
]

_OWNER_PATTERNS = [
    re.compile(r"\(\s*owner[:\s]+([^)]+?)\s*\)", re.IGNORECASE),
    re.compile(r"\bowner[:\s]+([A-Za-z0-9._\- ]+?)(?:\s*[,)]|$)", re.IGNORECASE),
]

_MENTION_RE = re.compile(r"(?<!\S)@([A-Za-z0-9._\-+]+(?:@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})?)")


def _extract_assignee(text: str) -> Optional[str]:
    for pattern in _OWNER_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group(1).strip()
    mention = _MENTION_RE.search(text)
    if mention:
        return mention.group(1).strip()
    return None


def _extract_due(text: str) -> Optional[str]:
    for pattern in _DUE_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group(1).strip()
    return None


def parse_action_items(md: str) -> List[ActionItem]:
    """Parse the ``Action Items`` section of a markdown document.

    Returns a list of :class:`ActionItem` dicts. If no action-items heading
    is found, returns an empty list.
    """
    if not md:
        return []

    lines = md.splitlines()
    in_section = False
    current_heading_level: Optional[int] = None
    items: List[ActionItem] = []

    for line in lines:
        heading = _HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).strip()
            if _ACTION_HEADING_RE.match(title):
                in_section = True
                current_heading_level = level
                continue
            # A heading of equal or shallower level closes the section.
            if in_section and current_heading_level is not None and level <= current_heading_level:
                in_section = False
                current_heading_level = None
            continue

        if not in_section:
            continue

        cb = _CHECKBOX_RE.match(line)
        if not cb:
            continue

        checked = cb.group(1).lower() == "x"
        text = cb.group(2).strip()
        items.append(
            {
                "text": text,
                "assignee_hint": _extract_assignee(text),
                "due_hint": _extract_due(text),
                "checked": checked,
            }
        )

    return items
