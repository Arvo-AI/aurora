"""
Content splitter for large memory entries.

When uploaded documents exceed MAX_PART_SIZE, they are split into
multiple entries at paragraph boundaries. Each part appears in the
memory index so the agent can discover and read all sections.
"""

import logging
import re

logger = logging.getLogger(__name__)

MAX_PART_SIZE = 50_000  # ~50KB per part, roughly 12-15 pages of text


def split_content(content: str, max_size: int = MAX_PART_SIZE) -> list[str]:
    """Split content into chunks at paragraph boundaries.

    Returns a list of content strings. If content fits within max_size,
    returns a single-element list (no splitting needed).
    """
    if len(content) <= max_size:
        return [content]

    paragraphs = re.split(r"\n{2,}", content)
    parts = []
    current_part = []
    current_size = 0

    for paragraph in paragraphs:
        para_size = len(paragraph) + 2  # +2 for the \n\n separator

        # If a single paragraph exceeds max_size, force-split it at line boundaries
        if para_size > max_size and not current_part:
            lines = paragraph.split("\n")
            for line in lines:
                if current_size + len(line) + 1 > max_size and current_part:
                    parts.append("\n".join(current_part))
                    current_part = []
                    current_size = 0
                current_part.append(line)
                current_size += len(line) + 1

            if current_part:
                parts.append("\n".join(current_part))
                current_part = []
                current_size = 0
            continue

        # Adding this paragraph would exceed the limit — start a new part
        if current_size + para_size > max_size and current_part:
            parts.append("\n\n".join(current_part))
            current_part = []
            current_size = 0

        current_part.append(paragraph)
        current_size += para_size

    # Don't forget the last part
    if current_part:
        parts.append("\n\n".join(current_part))

    return parts


def make_part_title(base_title: str, part_num: int, total_parts: int) -> str:
    """Generate a titled part like 'AWS Guide (2/5)'."""
    return f"{base_title} ({part_num}/{total_parts})"


def make_part_description(base_description: str, part_num: int, total_parts: int) -> str:
    """Generate description for a part, including the part indicator."""
    part_label = f"Part {part_num} of {total_parts}"
    if base_description:
        return f"{base_description} — {part_label}"
    return part_label
