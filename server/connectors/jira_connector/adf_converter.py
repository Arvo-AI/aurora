"""Markdown to Atlassian Document Format (ADF) converter.

Jira Cloud REST API v3 requires ADF for issue descriptions and comments.
This module converts basic markdown into the ADF JSON structure.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List


def text_to_adf(plain_text: str) -> Dict[str, Any]:
    """Wrap plain text in a minimal ADF document."""
    return {
        "version": 1,
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": plain_text}],
            }
        ],
    }


def _try_parse_link(text: str, start: int) -> tuple:
    """Try to parse a markdown link at ``text[start]`` (which should be '[').

    Returns ``(link_text, url, end_pos)`` on success or ``None`` if the
    text at *start* is not a valid ``[text](url)`` link.  Uses simple
    index scanning instead of regex to avoid polynomial backtracking.
    """
    close_bracket = text.find("]", start + 1)
    if close_bracket == -1:
        return None
    if close_bracket + 1 >= len(text) or text[close_bracket + 1] != "(":
        return None
    close_paren = text.find(")", close_bracket + 2)
    if close_paren == -1:
        return None
    link_text = text[start + 1 : close_bracket]
    url = text[close_bracket + 2 : close_paren]
    if not link_text or not url:
        return None
    return link_text, url, close_paren + 1


_SIMPLE_MARKS_RE = re.compile(
    r"(?P<bold_s>\*\*|__)"
    r"|(?P<italic_s>[*_])"
    r"|(?P<code>`)"
    r"|(?P<bracket>\[)"
)


def _inline_marks(text: str) -> List[Dict[str, Any]]:
    """Parse inline markdown (bold, italic, code, links) into ADF inline nodes."""
    nodes: List[Dict[str, Any]] = []
    pos = 0

    while pos < len(text):
        m = _SIMPLE_MARKS_RE.search(text, pos)
        if not m:
            remainder = text[pos:]
            if remainder:
                nodes.append({"type": "text", "text": remainder})
            break

        if m.start() > pos:
            nodes.append({"type": "text", "text": text[pos:m.start()]})

        if m.group("bracket"):
            link = _try_parse_link(text, m.start())
            if link:
                link_text, url, end_pos = link
                nodes.append({
                    "type": "text",
                    "text": link_text,
                    "marks": [{"type": "link", "attrs": {"href": url}}],
                })
                pos = end_pos
            else:
                nodes.append({"type": "text", "text": "["})
                pos = m.start() + 1

        elif m.group("code"):
            end = text.find("`", m.end())
            if end == -1:
                nodes.append({"type": "text", "text": text[m.start():]})
                break
            nodes.append({
                "type": "text",
                "text": text[m.end():end],
                "marks": [{"type": "code"}],
            })
            pos = end + 1

        elif m.group("bold_s"):
            marker = m.group("bold_s")
            end = text.find(marker, m.end())
            if end == -1:
                nodes.append({"type": "text", "text": text[m.start():]})
                break
            nodes.append({
                "type": "text",
                "text": text[m.end():end],
                "marks": [{"type": "strong"}],
            })
            pos = end + len(marker)

        elif m.group("italic_s"):
            marker = m.group("italic_s")
            end = text.find(marker, m.end())
            if end == -1:
                nodes.append({"type": "text", "text": text[m.start():]})
                break
            nodes.append({
                "type": "text",
                "text": text[m.end():end],
                "marks": [{"type": "em"}],
            })
            pos = end + len(marker)
        else:
            pos = m.end()

    return nodes


def _parse_line_to_inline(line: str) -> List[Dict[str, Any]]:
    """Convert a markdown line to ADF inline content nodes."""
    nodes = _inline_marks(line)
    return nodes if nodes else [{"type": "text", "text": line or " "}]


def markdown_to_adf(markdown_text: str) -> Dict[str, Any]:
    """Convert markdown text to an ADF document.

    Handles headings, paragraphs, bullet lists, ordered lists, code blocks,
    task lists (``- [ ]`` / ``- [x]``), bold, italic, inline code, and links.
    """
    lines = markdown_text.split("\n")
    content: List[Dict[str, Any]] = []
    i = 0

    while i < len(lines):
        line = lines[i]

        # Fenced code block
        if line.startswith("```"):
            language = line[3:].strip() or None
            code_lines: List[str] = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # skip closing ```
            node: Dict[str, Any] = {
                "type": "codeBlock",
                "content": [{"type": "text", "text": "\n".join(code_lines)}],
            }
            if language:
                node["attrs"] = {"language": language}
            content.append(node)
            continue

        # Heading
        heading_match = re.match(r"^(#{1,6})\s+(.*)", line)
        if heading_match:
            level = len(heading_match.group(1))
            content.append({
                "type": "heading",
                "attrs": {"level": level},
                "content": _parse_line_to_inline(heading_match.group(2)),
            })
            i += 1
            continue

        # Task list item: - [ ] or - [x]
        task_match = re.match(r"^[-*]\s+\[([ xX])\]\s+(.*)", line)
        if task_match:
            items: List[Dict[str, Any]] = []
            while i < len(lines):
                tm = re.match(r"^[-*]\s+\[([ xX])\]\s+(.*)", lines[i])
                if not tm:
                    break
                checked = tm.group(1).lower() == "x"
                items.append({
                    "type": "taskItem",
                    "attrs": {"state": "DONE" if checked else "TODO"},
                    "content": _parse_line_to_inline(tm.group(2)),
                })
                i += 1
            content.append({"type": "taskList", "content": items})
            continue

        # Unordered list
        ul_match = re.match(r"^[-*]\s+(.*)", line)
        if ul_match:
            items = []
            while i < len(lines):
                um = re.match(r"^[-*]\s+(.*)", lines[i])
                if not um:
                    break
                items.append({
                    "type": "listItem",
                    "content": [{"type": "paragraph", "content": _parse_line_to_inline(um.group(1))}],
                })
                i += 1
            content.append({"type": "bulletList", "content": items})
            continue

        # Ordered list
        ol_match = re.match(r"^\d+[.)]\s+(.*)", line)
        if ol_match:
            items = []
            while i < len(lines):
                om = re.match(r"^\d+[.)]\s+(.*)", lines[i])
                if not om:
                    break
                items.append({
                    "type": "listItem",
                    "content": [{"type": "paragraph", "content": _parse_line_to_inline(om.group(1))}],
                })
                i += 1
            content.append({"type": "orderedList", "content": items})
            continue

        # Blank line
        if not line.strip():
            i += 1
            continue

        # Regular paragraph
        content.append({
            "type": "paragraph",
            "content": _parse_line_to_inline(line),
        })
        i += 1

    if not content:
        content.append({
            "type": "paragraph",
            "content": [{"type": "text", "text": " "}],
        })

    return {"version": 1, "type": "doc", "content": content}


def extract_action_items(markdown_text: str) -> List[Dict[str, Any]]:
    """Extract ``- [ ]`` task list items from markdown.

    Returns a list of dicts with keys ``text`` and ``checked``.
    """
    items: List[Dict[str, Any]] = []
    for line in markdown_text.split("\n"):
        m = re.match(r"^[-*]\s+\[([ xX])\]\s+(.*)", line)
        if m:
            items.append({
                "text": m.group(2).strip(),
                "checked": m.group(1).lower() == "x",
            })
    return items
